"""The customer service desk: shared state, three agents, three tools, a retriever, and how they connect.

A customer writes in. The desk handles the message in up to four steps:

  support_triage   reads the message, works out what the customer wants, and
                   looks the customer up (tool: lookup_customer)
  order_desk       looks up the order, changes the delivery address if asked
                   (tools: find_order, update_address), checks the staff
                   handbook (a retriever), and writes the reply
  escalation       complaints only: writes a summary for the store manager
  internal_notes   complaints only: writes staff-only notes

The messages are full of personal data: email addresses, phone numbers, card
numbers, street addresses. The agents need all of it to do their job. What
reaches AIO is decided by the redaction policy in observability.py, not here.

All lookups and decisions are plain Python. The models only choose the tool
calls and write the human-readable text.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TypedDict

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.retrievers import BaseRetriever
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph

from models import FixtureChatModel, Reply
from observability import hide_arguments, private_model_inputs, record_node

DATA_DIR = Path(__file__).parent / "data"

STORE_EMAIL = "support@bookstore.example"


# --- The store's data --------------------------------------------------------


def load_store() -> dict:
    """Read the customers, orders and staff handbook in data/ into one dict."""
    return {name: json.loads((DATA_DIR / f"{name}.json").read_text()) for name in ("customers", "orders", "handbook")}


class StaffHandbook(BaseRetriever):
    """The store's staff handbook: internal policy passages, found by topic.

    A real LangChain retriever, so each search is traced as a retrieval span:
    the query is its input and the passages found are its output.
    """

    passages: list[dict]

    def _get_relevant_documents(self, query: str, *, run_manager: CallbackManagerForRetrieverRun) -> list[Document]:
        words = set(query.lower().split())
        return [
            Document(page_content=passage["text"], metadata={"id": passage["id"]})
            for passage in self.passages
            if words & set(passage["topic"].split())
        ]


# What the Order Desk searches the handbook for. The query names a topic, never
# the customer's own words, so it carries no personal data.
HANDBOOK_QUERIES = {
    "order-status": "late order credit",
    "address-change": "address change",
    "refund-request": "refund damaged order",
    "complaint": "complaint escalation late",
}


# --- Shared state --------------------------------------------------------------


class DeskState(TypedDict, total=False):
    message: str           # what the customer wrote
    intent: str            # "order-status", "address-change", "refund-request" or "complaint"
    customer_id: str       # written by Support Triage
    first_name: str        # written by Support Triage
    order_number: str      # written by Support Triage
    reply: str             # the Order Desk's answer to the customer (model call)
    manager_summary: str   # complaints only: Escalation's summary (model call)
    staff_notes: str       # complaints only: written by internal_notes, never traced
    outcome: str           # "answered", "escalated" or "unknown-customer"


# --- Reading the customer's message ---------------------------------------------

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"\b\d{3}-\d{3}-\d{4}\b")
ORDER_NUMBER = re.compile(r"(?:order\s*#?|#)(\d{4})\b", re.IGNORECASE)
# An address starts with a house number: "48 Harbour Road, Apt 3, Portland".
NEW_ADDRESS = re.compile(r"(?:to|address is|address:)\s+(\d+\s[^.!?]+?)(?:\s+instead)?(?=[.!?]|$)", re.IGNORECASE)

# Checked in this order: a complaint about a refund is still a complaint.
INTENT_WORDS = {
    "complaint": ("manager", "complain", "unacceptable", "third time", "ridiculous", "fed up"),
    "refund-request": ("refund", "return", "damaged", "torn", "broken"),
    "address-change": ("moved", "new address", "change the address", "change my address", "deliver it to", "send it to"),
}


def intent_of(message: str) -> str:
    """What the customer wants. Anything that is not a complaint, refund or address change is an order-status question."""
    text = message.lower()
    for intent, words in INTENT_WORDS.items():
        if any(word in text for word in words):
            return intent
    return "order-status"


# --- What each agent tells its model -------------------------------------------
# The Order Desk's prompt holds the store's internal rules. That is why the
# policy hides system prompts: they are the store's, not the customer's, but
# they still must not leave the app.

TRIAGE_PROMPT = """You are Support Triage for a small independent bookstore.
Read the customer's message and call lookup_customer with whatever identifies
them: their email, their phone number or an order number. Never repeat card
numbers back to anyone."""

ORDER_DESK_PROMPT = f"""You are the Order Desk for a small independent bookstore.
First call find_order. If the customer has moved and the order has not shipped,
call update_address. Then write a short, friendly reply.

Internal rules, never to be quoted to customers:
- Late more than 5 days: add a 10% store credit, up to $15, without being asked.
- Damaged on arrival: refund in full, no return needed, if reported within 30 days.
- Never offer more than 20% off. Anything bigger needs the store manager.
End every reply with: Questions? Write to {STORE_EMAIL}."""

ESCALATION_PROMPT = """You are Escalation for a small independent bookstore.
Read the customer's complaint and their order, and write a two-line summary for
the store manager: what went wrong and what to do today. Leave out personal
details: the manager has the customer record."""


def last_facts(messages: list[BaseMessage]) -> dict:
    """The JSON facts an agent put in its last message to the model."""
    return json.loads(messages[-1].content)


def triage_respond(messages: list[BaseMessage]) -> Reply:
    """What the fixture model does for Support Triage: call lookup_customer."""
    text = messages[-1].content
    args = {}
    if match := EMAIL.search(text):
        args["email"] = match.group(0)
    if match := PHONE.search(text):
        args["phone"] = match.group(0)
    if match := ORDER_NUMBER.search(text):
        args["order_number"] = match.group(1)
    return "Looking up the customer.", [{"name": "lookup_customer", "args": args}]


def desk_tool_calls(facts: dict) -> list[dict]:
    """The Order Desk's first model call: which tools to call."""
    calls = [{"name": "find_order", "args": {"customer_id": facts["customer_id"], "order_number": facts["order_number"]}}]
    if facts.get("new_address"):
        calls.append(
            {
                "name": "update_address",
                "args": {"customer_id": facts["customer_id"], "order_number": facts["order_number"], "new_address": facts["new_address"]},
            }
        )
    return calls


def desk_reply(facts: dict) -> str:
    """The Order Desk's second model call: the reply to the customer, from a template."""
    order, name, number = facts["order"], facts["first_name"], facts["order_number"]
    items = ", ".join(order["items"])
    intent = facts["intent"]
    if intent == "refund-request":
        body = f"I'm sorry your copy of {items} arrived damaged. We've refunded ${order['total']:.2f} to your original payment method; there's no need to send it back."
    elif intent == "address-change" and not facts.get("new_address_given"):
        body = f"please reply with the new address for order #{number}, and we'll change it if it hasn't shipped."
    elif intent == "address-change" and facts.get("address_updated"):
        body = f"your order #{number} ({items}) hasn't shipped yet, so we've changed its delivery address to the one you sent."
    elif intent == "address-change":
        body = f"your order #{number} ({items}) has already left us, so we can't change its address any more."
    elif intent == "complaint":
        body = f"I'm very sorry about order #{number}. I've passed this to our store manager, who will contact you within one business day."
    else:
        body = f"your order #{number} ({items}) is {order['status']}."
    if order["days_late"] > 5:
        credit = min(round(order["total"] * 0.10, 2), 15)
        body += f" It's {order['days_late']} days late, so we've added ${credit:.2f} of store credit to your account."
    return f"Hi {name}, {body}\nQuestions? Write to {STORE_EMAIL}."


def desk_respond(messages: list[BaseMessage]) -> Reply:
    facts = last_facts(messages)
    if "order" not in facts:
        return "Checking the order.", desk_tool_calls(facts)
    return desk_reply(facts), []


def escalation_respond(messages: list[BaseMessage]) -> Reply:
    facts = last_facts(messages)
    order = facts["order"]
    return (
        f"Repeat complaint about order #{facts['order_number']}: {order['status']}, {order['days_late']} days late. "
        "The customer asked for a manager.\nCall them today and refund the order in full.",
        [],
    )


# --- The graph -------------------------------------------------------------------


def build_graph(store: dict | None = None):
    """Wire the agents, the tools and the steps into a LangGraph graph."""
    store = store or load_store()

    @tool
    def lookup_customer(email: str = "", phone: str = "", order_number: str = "") -> dict:
        """Find a customer by email, phone number or order number. Returns their whole record."""
        for customer_id, customer in store["customers"].items():
            if (email and customer["email"] == email) or (phone and customer["phone"] == phone):
                return {"customer_id": customer_id, **customer}
        order = store["orders"].get(order_number)
        if order:
            customer_id = order["customer_id"]
            return {"customer_id": customer_id, **store["customers"][customer_id]}
        return {}

    @tool
    def find_order(customer_id: str, order_number: str) -> dict:
        """Find one of the customer's orders: its items, total, status and how many days late it is."""
        order = store["orders"].get(order_number)
        if not order or order["customer_id"] != customer_id:
            return {}
        return {"order_number": order_number, **order}

    # Marked in its own code, so the new address never reaches AIO through
    # this tool, whatever the policy says.
    @hide_arguments
    @tool
    def update_address(customer_id: str, order_number: str, new_address: str) -> dict:
        """Change the delivery address of an order that has not shipped yet."""
        order = store["orders"].get(order_number)
        if not order or order["customer_id"] != customer_id or order["shipped"]:
            return {"updated": False}
        return {"updated": True}

    tools = {t.name: t for t in (lookup_customer, find_order, update_address)}
    handbook = StaffHandbook(passages=store["handbook"], name="staff_handbook")

    def run_tools(reply) -> dict:
        """Run the tool calls a model asked for. Returns each tool's result by name."""
        return {call["name"]: tools[call["name"]].invoke(call["args"]) for call in reply.tool_calls}

    @record_node(input=lambda state: state["message"])
    def support_triage(state: DeskState) -> dict:
        """Agent 1: work out what the customer wants, and who they are."""
        model = FixtureChatModel(respond=triage_respond).bind_tools([lookup_customer])
        reply = model.invoke([SystemMessage(TRIAGE_PROMPT), HumanMessage(state["message"])])
        customer = run_tools(reply)["lookup_customer"]
        order_number = ORDER_NUMBER.search(state["message"])
        # The handoff. Only what the Order Desk needs goes on: not the email,
        # phone or home address that lookup_customer returned.
        return {
            "intent": intent_of(state["message"]),
            "customer_id": customer.get("customer_id", ""),
            "first_name": customer.get("name", "").split(" ")[0],
            "order_number": order_number.group(1) if order_number else "",
        }

    @record_node(input=lambda state: {key: state.get(key) for key in ("intent", "customer_id", "order_number")})
    def order_desk(state: DeskState) -> dict:
        """Agent 2: look up the order, act on it, and write the reply."""
        model = FixtureChatModel(respond=desk_respond).bind_tools([find_order, update_address])
        facts = {key: state[key] for key in ("intent", "customer_id", "first_name", "order_number")}
        if state["intent"] == "address-change" and (match := NEW_ADDRESS.search(state["message"])):
            facts["new_address"] = match.group(1)

        results = run_tools(model.invoke([SystemMessage(ORDER_DESK_PROMPT), HumanMessage(json.dumps(facts))]))
        facts["order"] = results["find_order"]
        facts["address_updated"] = results.get("update_address", {}).get("updated", False)
        facts["new_address_given"] = bool(facts.pop("new_address", None))
        if not facts["order"]:
            return {"reply": f"Sorry, we couldn't find that order. Please write to {STORE_EMAIL}.", "outcome": "unknown-customer"}

        # The passages are internal. Only their IDs go to the model, so their
        # text reaches AIO in one place: the retrieval span, which the policy hides.
        facts["handbook"] = [doc.metadata["id"] for doc in handbook.invoke(HANDBOOK_QUERIES[state["intent"]])]

        reply = model.invoke([SystemMessage(ORDER_DESK_PROMPT), HumanMessage(json.dumps(facts))])
        return {"reply": reply.content, "outcome": "answered"}

    @record_node(input=lambda state: {"order_number": state["order_number"]})
    def escalation(state: DeskState) -> dict:
        """Agent 3, complaints only: a summary for the store manager.

        Its model reads the whole complaint, so everything sent to it is kept
        out of AIO. Its summary, the reply, is still traced.
        """
        model = FixtureChatModel(respond=escalation_respond)
        facts = {"complaint": state["message"], "order_number": state["order_number"], "order": store["orders"][state["order_number"]]}
        with private_model_inputs():
            reply = model.invoke([SystemMessage(ESCALATION_PROMPT), HumanMessage(json.dumps(facts))])
        return {"manager_summary": reply.content, "outcome": "escalated"}

    # No @record_node here. The policy stops this node's span being created,
    # but a node's recorded input and output are sent separately, so they
    # would still reach AIO.
    def internal_notes(state: DeskState) -> dict:
        """Staff-only notes: the whole message and the customer's record. Never traced."""
        customer = store["customers"][state["customer_id"]]
        return {"staff_notes": f"{customer['name']}, {customer['phone']}, {customer['address']}. They wrote: {state['message']}"}

    def after_order_desk(state: DeskState) -> str:
        return "escalation" if state["intent"] == "complaint" and state["outcome"] == "answered" else END

    builder = StateGraph(DeskState)
    builder.add_node("support_triage", support_triage)
    builder.add_node("order_desk", order_desk)
    builder.add_node("escalation", escalation)
    builder.add_node("internal_notes", internal_notes)
    builder.add_edge(START, "support_triage")
    builder.add_edge("support_triage", "order_desk")
    builder.add_conditional_edges("order_desk", after_order_desk, ["escalation", END])
    builder.add_edge("escalation", "internal_notes")
    builder.add_edge("internal_notes", END)
    return builder.compile(name="customer_desk")
