"""Schema-validated structured LLM outputs. The LLM proposes; code decides."""
from __future__ import annotations

from typing import Literal, Optional, Union

from pydantic import BaseModel, Field

FieldName = Literal[
    "company_name", "domain", "product", "business_model", "customer_segments", "named_customers",
    "funding_stage", "employee_count", "data_handled", "compliance_claims", "hiring_signals",
    "business_triggers", "existing_compliance_tools",
]
FIELD_NAMES: tuple[str, ...] = FieldName.__args__  # type: ignore[attr-defined]
LIST_FIELDS = {"customer_segments", "named_customers", "data_handled", "compliance_claims",
               "hiring_signals", "business_triggers", "existing_compliance_tools"}

Dimension = Literal["business_fit", "customer_trust", "compliance_workload", "reason_now", "delivery_complexity"]
DIMENSIONS: tuple[str, ...] = Dimension.__args__  # type: ignore[attr-defined]


# ---------------- extraction
class Citation(BaseModel):
    url: str
    excerpt: str = Field(description="Exact verbatim excerpt from the page text")
    published_at: Optional[str] = Field(default=None, description="YYYY-MM-DD if the page dates this statement")


class FieldExtraction(BaseModel):
    name: FieldName
    value: Union[str, list[str], None]
    status: Literal["observed", "inferred", "unknown", "conflicting"]
    citations: list[Citation]


class ExtractionOutput(BaseModel):
    fields: list[FieldExtraction]


# ---------------- qualification
class DimensionProposal(BaseModel):
    dimension: Dimension
    level: Literal[0, 1, 2]
    evidence_ids: list[str]
    zero_reason: Optional[Literal["unknown", "explicit_negative", "outside_window"]] = None
    reason: str


class EvidenceGap(BaseModel):
    dimension: Dimension
    description: str
    page_hints: list[str]


class QualificationProposal(BaseModel):
    dimensions: list[DimensionProposal]
    disqualifier: Literal["none", "unrelated_business", "compliance_platform_competitor"]
    disqualifier_evidence_ids: list[str]
    gaps: list[EvidenceGap]


class NextPagesProposal(BaseModel):
    gap_dimension: Optional[Dimension]
    gap_description: str
    urls: list[str]
    reason: str


# ---------------- pain
class PainLevels(BaseModel):
    evidence: Literal[0, 1, 2, 3]
    recipient: Literal[0, 1, 2]
    consequence: Literal[0, 1, 2]
    capability: Literal[0, 1, 2]
    honest: Literal[0, 1]


class PainProposal(BaseModel):
    observed_fact: str
    evidence_ids: list[str]
    hypothesis: str
    role_relevance: str
    possible_business_consequence: str
    relevant_capability: str
    levels: PainLevels


class PainOutput(BaseModel):
    hypotheses: list[PainProposal]


# ---------------- email
class Claim(BaseModel):
    text: str
    evidence_ids: list[str]


class EmailDraft(BaseModel):
    """The assembled email (code builds `body` from EmailParts; checks run on this)."""
    subject: str
    body: str
    claims: list[Claim]
    capability_id: str


class EmailParts(BaseModel):
    """What the LLM writes. Code assembles the layout so every email has the same structure."""
    subject: str
    greeting: str = Field(description="Greeting line only, e.g. 'Hi Alex,'")
    opening: str = Field(description="1-2 sentences: the verified company fact")
    paragraphs: list[str] = Field(description="1-2 short paragraphs: the tentative pain, then the approved capability")
    question: str = Field(description="The single closing question")
    claims: list[Claim]
    capability_id: str


class EmailEvaluation(BaseModel):
    specific_hook: Literal[0, 1, 2]
    useful_hypothesis: Literal[0, 1, 2]
    relevant_value: Literal[0, 1, 2]
    simple_next_step: Literal[0, 1, 2]
    clarity: Literal[0, 1, 2]
    issues: list[str]


# ---------------- replies
ReplyIntent = Literal[
    "interested_meeting", "info_request", "confirm_slot", "counterproposal", "reject", "opt_out",
    "referral", "out_of_office", "delivery_notice", "unsupported_request", "ambiguous",
]


class ReplyClassification(BaseModel):
    intent: ReplyIntent
    slot_reference: Optional[str]
    proposed_time_text: Optional[str]
    timezone_text: Optional[str]
    referral_name: Optional[str]
    referral_title: Optional[str]
    question_summary: Optional[str]
    preferred_window_text: Optional[str] = Field(
        default=None, description="Verbatim timing preference without an exact time, e.g. 'next week', 'Tuesday afternoon'")
    reason: str


class CompanyEvidenceRef(BaseModel):
    citation_index: int
    signal: Literal["funding", "hiring", "nrr", "headcount", "industry", "launch", "compliance", "other"]


class DiscoveredCompany(BaseModel):
    name: str
    domain: Optional[str] = Field(default=None, description="Only if the domain is written in a cited source")
    evidence: list[CompanyEvidenceRef]
    funding_series: Optional[str] = Field(default=None, description="Exact wording, e.g. 'Series B'")
    funding_date: Optional[str] = Field(default=None, description="YYYY-MM-DD if stated")
    nrr_text: Optional[str] = Field(default=None, description="Exact NRR/NDR wording if stated, e.g. '125% net revenue retention'")
    headcount_text: Optional[str] = None


class DiscoveredCompanies(BaseModel):
    companies: list[DiscoveredCompany]


class InfoAnswer(BaseModel):
    capability_ids: list[str]
    unanswerable: bool
    reason: str


class AssistantStep(BaseModel):
    """One step of the project assistant: call a tool, or answer the user."""
    on_topic: bool = Field(description="false if the user's message is not about this prospecting project")
    tool: Optional[str] = Field(default=None, description="name of the tool to call next; null to answer now")
    args_json: str = Field(default="{}", description="tool arguments as a JSON object string")
    answer: Optional[str] = Field(default=None, description="the reply to the user when tool is null")


class FollowUpDraft(BaseModel):
    subject: str
    body: str
