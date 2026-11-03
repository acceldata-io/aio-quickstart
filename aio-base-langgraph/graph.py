"""The bookstore graph: shared state, two agents, one tool, and how they connect.

Two agents work together on one question from the store manager:

  inventory_analyst     reads sales and stock, finds books that are running low
  supplier_coordinator  asks the supplier about those books and writes the
                        reorder recommendation

The analyst hands its findings to the coordinator only through the shared
state (`findings`), so the handoff is easy to spot here and in the trace.

All stock maths and supplier results are plain Python. The models only write
the human-readable text.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph

from models import FixtureChatModel
from observability import record_node

DATA_DIR = Path(__file__).parent / "data"

MAX_SUPPLIER_ATTEMPTS = 2
RETRY_BACKOFF_SECONDS = 1.0
SLOW_SUPPLIER_SECONDS = 6.0  # how long the supplier waits in the slow-supplier scenario


# --- Failure injection: every simulated problem lives here ----------------------


@dataclass(frozen=True)
class Faults:
    """The problems a scenario switches on. The default is no problems at all."""

    supplier_fails_on: frozenset[int] = frozenset()  # attempt numbers where the supplier times out
    slow_supplier: bool = False                      # supplier waits SLOW_SUPPLIER_SECONDS before answering
    coordinator_reply: str | None = None             # fixed text the coordinator's model returns


# banned-term and empty-output replace the coordinator's model reply with a fixed bad answer.
# agent-retry makes the first supplier lookup time out.
# slow-supplier makes the supplier answer slowly.
FAULTS: dict[str, Faults] = {
    "happy-path": Faults(),
    "banned-term": Faults(
        coordinator_reply=(
            "Recommended reorders:\n"
            "- Order 26 x The Midnight Library from Harbour Book Distributors at orders@harbourbooks.example (arrives in about 3 days)\n"
            "If that is too slow, MegaBooks has it in stock and could ship it tomorrow."
        )
    ),
    "empty-output": Faults(coordinator_reply="   \n"),
    "agent-retry": Faults(supplier_fails_on=frozenset({1})),
    "slow-supplier": Faults(slow_supplier=True),
    "repeat-questions": Faults(),
}


class SupplierTimeout(Exception):
    """The supplier did not answer in time (always simulated in this demo)."""


# --- The store's data --------------------------------------------------------


def load_store() -> dict:
    """Read the four JSON files in data/ into one dict."""
    store = {name: json.loads((DATA_DIR / f"{name}.json").read_text()) for name in ("books", "inventory", "sales", "suppliers")}
    store["books"] = {book["book_id"]: book for book in store["books"]}
    store["sales"].pop("_note", None)
    return store


# --- Shared state --------------------------------------------------------------


class Finding(TypedDict):
    """One low-stock book, as found by the Inventory Analyst."""

    book_id: str
    title: str
    on_hand: int
    reorder_threshold: int
    sold_last_7_days: int
    order_quantity: int  # enough for two weeks of sales


class ShopState(TypedDict, total=False):
    question: str                 # what the manager asked
    findings: list[Finding]       # written by the Inventory Analyst
    summary: str                  # the analyst's short write-up (model call)
    supplier_result: dict | None  # written by the Supplier Coordinator
    attempts: int                 # how many supplier lookups were made
    recommendation: str           # the coordinator's final text (model call)
    errors: list[str]             # anything that went wrong, kept for debugging
    outcome: str                  # "ordered", "nothing_to_order" or "failed"


# --- What each agent tells its model -------------------------------------------

ANALYST_PROMPT = """You are the Inventory Analyst for a small independent bookstore.
You receive the manager's question and a JSON list of books that are at or below
their reorder threshold. Write a short summary for the manager: which books are
running low and how fast they are selling. Use only the numbers you are given.
If the list is empty, say that nothing needs reordering."""

COORDINATOR_PROMPT = """You are the Supplier Coordinator for a small independent bookstore.
You receive the analyst's low-stock findings and the supplier's answer as JSON.
Write the reorder recommendation for the manager: one line per book with the
quantity to order, the supplier, the lead time in days and the supplier's order email. Use only the numbers
you are given. Do not mention or recommend other bookstores."""


def analyst_reply(facts: dict) -> str:
    """What the fixture model writes for the analyst: a template, not an LLM."""
    if not facts["findings"]:
        return "Nothing you asked about is at or below its reorder threshold, so there is nothing to reorder."
    lines = [
        f"- {f['title']}: {f['on_hand']} left (reorder at {f['reorder_threshold']}), {f['sold_last_7_days']} sold this week"
        for f in facts["findings"]
    ]
    return f"{len(lines)} book(s) are running low:\n" + "\n".join(lines)


def coordinator_reply(facts: dict) -> str:
    """What the fixture model writes for the coordinator: a template, not an LLM."""
    lines = [
        f"- Order {o['quantity']} x {o['title']} from {o['supplier']} at {o['email']} (arrives in about {o['lead_days']} days)"
        for o in facts["orders"]
    ]
    return "Recommended reorders:\n" + "\n".join(lines)


def make_models() -> tuple[BaseChatModel, BaseChatModel]:
    """The analyst's and the coordinator's models: fixtures that fill in a template."""
    return (
        FixtureChatModel(writer=analyst_reply),
        FixtureChatModel(writer=coordinator_reply),
    )


# --- The graph -------------------------------------------------------------------


def key_words(text: str) -> set[str]:
    """The words that can identify a book: lower case, longer than 3 letters, not "with"."""
    return {word for word in re.findall(r"[a-z]+", text.lower()) if len(word) > 3 and word != "with"}


def mentions_title(question_words: set[str], title: str) -> bool:
    """True if the question names this title, even in part ("Harry Potter", "Sapiens").

    A one-word title must appear as is. A longer one needs two of its words.
    """
    for name in (title, title.split(":")[0]):  # the full title, or the part before a colon
        words = key_words(name)
        if words and len(words & question_words) >= min(2, len(words)):
            return True
    return False


def books_asked_about(question: str, store: dict) -> list[str]:
    """Work out which books the question is about.

    Book titles in the question win, then a shelf name ("sci-fi", "mystery"),
    otherwise the whole store.
    """
    question_words = key_words(question)
    named = [book_id for book_id, book in store["books"].items() if mentions_title(question_words, book["title"])]
    if named:
        return named
    text = question.lower()
    shelves = {book["shelf"] for book in store["books"].values()}
    for shelf in shelves:
        if shelf in text or (shelf == "sci-fi" and "science fiction" in text):
            return [book_id for book_id, book in store["books"].items() if book["shelf"] == shelf]
    return list(store["books"])


def build_graph(
    analyst_model: BaseChatModel,
    coordinator_model: BaseChatModel,
    faults: Faults = Faults(),
    store: dict | None = None,
):
    """Wire the two agents, the tool and the checks into a LangGraph graph.

    `faults` says which simulated problems to switch on for this run.
    """
    store = store or load_store()
    if faults.coordinator_reply is not None:
        # Swap in a fixture that always gives the scenario's bad answer.
        coordinator_model = FixtureChatModel(canned_reply=faults.coordinator_reply)

    @tool
    def read_stock(book_ids: list[str]) -> dict:
        """Read the copies on hand, the reorder threshold and the copies sold in the last 7 days for each book."""
        return {
            book_id: {**store["inventory"][book_id], "sold_last_7_days": sum(store["sales"][book_id])}
            for book_id in book_ids
        }

    @tool
    def check_supplier(book_ids: list[str], attempt: int) -> dict:
        """Ask the supplier how many copies of each book they have and how many days delivery takes.

        `attempt` is the lookup number (1, 2, ...), passed so it shows on the trace.
        """
        if faults.slow_supplier:
            time.sleep(SLOW_SUPPLIER_SECONDS)
        if attempt in faults.supplier_fails_on:
            raise SupplierTimeout(f"Supplier did not answer on attempt {attempt} (simulated timeout)")
        return {book_id: store["suppliers"][book_id] for book_id in book_ids}

    @record_node(input=lambda state: state["question"])
    def inventory_analyst(state: ShopState) -> dict:
        """Agent 1: find low-stock books and summarise them."""
        findings: list[Finding] = []
        stock_levels = read_stock.invoke({"book_ids": books_asked_about(state["question"], store)})
        for book_id, stock in stock_levels.items():
            sold = stock["sold_last_7_days"]
            if stock["on_hand"] <= stock["reorder_threshold"]:
                findings.append(
                    Finding(
                        book_id=book_id,
                        title=store["books"][book_id]["title"],
                        on_hand=stock["on_hand"],
                        reorder_threshold=stock["reorder_threshold"],
                        sold_last_7_days=sold,
                        order_quantity=max(sold * 2 - stock["on_hand"], 1),
                    )
                )
        facts = {"question": state["question"], "findings": findings}
        reply = analyst_model.invoke([SystemMessage(ANALYST_PROMPT), HumanMessage(json.dumps(facts, indent=2))])
        # The handoff: the coordinator only sees what is written here.
        return {"findings": findings, "summary": reply.content}

    @record_node(input=lambda state: state["findings"])
    def supplier_coordinator(state: ShopState) -> dict:
        """Agent 2: check the supplier for the analyst's books and write the recommendation."""
        findings = state["findings"]
        errors = list(state.get("errors", []))

        # The retry loop. Each attempt is its own tool call, so a
        # failed attempt shows up as its own span, marked as an error.
        answer = None
        for attempt in range(1, MAX_SUPPLIER_ATTEMPTS + 1):
            try:
                answer = check_supplier.invoke({"book_ids": [f["book_id"] for f in findings], "attempt": attempt})
                break
            except SupplierTimeout as error:
                errors.append(str(error))
                if attempt < MAX_SUPPLIER_ATTEMPTS:
                    time.sleep(RETRY_BACKOFF_SECONDS)
        if answer is None:
            errors.append(f"Supplier lookup failed after {MAX_SUPPLIER_ATTEMPTS} attempts.")
            return {"supplier_result": None, "attempts": attempt, "errors": errors}

        orders = [
            {
                "book_id": f["book_id"],
                "title": f["title"],
                "quantity": min(f["order_quantity"], answer[f["book_id"]]["stock"]),
                "supplier": answer[f["book_id"]]["supplier"],
                "email": answer[f["book_id"]]["email"],
                "lead_days": answer[f["book_id"]]["lead_days"],
            }
            for f in findings
        ]
        supplier_result = {"orders": orders}
        facts = {"question": state["question"], "findings": findings, "orders": orders}
        reply = coordinator_model.invoke([SystemMessage(COORDINATOR_PROMPT), HumanMessage(json.dumps(facts, indent=2))])
        return {"supplier_result": supplier_result, "attempts": attempt, "errors": errors, "recommendation": reply.content}

    @record_node(input=lambda state: {"recommendation": state.get("recommendation"), "orders": state["supplier_result"]["orders"]})
    def validate(state: ShopState) -> dict:
        """Check the recommendation before it reaches the manager."""
        problems = []
        if not state.get("recommendation", "").strip():
            problems.append("The recommendation is empty.")
        for order in state["supplier_result"]["orders"]:
            if order["book_id"] not in store["books"]:
                problems.append(f"Unknown book ID {order['book_id']}.")
            if not isinstance(order["quantity"], int) or order["quantity"] <= 0:
                problems.append(f"Bad quantity {order['quantity']!r} for {order['book_id']}.")
        if problems:
            return {"errors": state.get("errors", []) + problems, "outcome": "failed"}
        return {}

    @record_node(input=lambda state: {"findings": state.get("findings"), "errors": state.get("errors")})
    def report(state: ShopState) -> dict:
        """The normal ending: the manager gets an answer."""
        return {"outcome": "ordered" if state.get("findings") else "nothing_to_order"}

    @record_node(input=lambda state: {"errors": state.get("errors")})
    def failure_report(state: ShopState) -> dict:
        """The failure ending: the manager is told the run could not finish."""
        return {"outcome": "failed"}

    # The three decisions in the graph, one per conditional edge.

    def after_analyst(state: ShopState) -> str:
        return "supplier_coordinator" if state["findings"] else "report"

    def after_coordinator(state: ShopState) -> str:
        return "validate" if state["supplier_result"] is not None else "failure_report"

    def after_validate(state: ShopState) -> str:
        return "failure_report" if state.get("outcome") == "failed" else "report"

    builder = StateGraph(ShopState)
    builder.add_node("inventory_analyst", inventory_analyst)
    builder.add_node("supplier_coordinator", supplier_coordinator)
    builder.add_node("validate", validate)
    builder.add_node("report", report)
    builder.add_node("failure_report", failure_report)
    builder.add_edge(START, "inventory_analyst")
    builder.add_conditional_edges("inventory_analyst", after_analyst, ["supplier_coordinator", "report"])
    builder.add_conditional_edges("supplier_coordinator", after_coordinator, ["validate", "failure_report"])
    builder.add_conditional_edges("validate", after_validate, ["report", "failure_report"])
    builder.add_edge("report", END)
    builder.add_edge("failure_report", END)
    return builder.compile(name="bookstore")
