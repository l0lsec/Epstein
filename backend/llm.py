"""
LLM Integration Module
Provides intelligent Q&A and analysis over documents
"""

import os
from typing import List, Dict, Any, Optional, Callable
from openai import OpenAI


# OpenAI API pricing in USD per 1,000,000 tokens as (input, output).
# Models are matched by LONGEST prefix so dated variants (e.g.
# "gpt-4o-2024-08-06") resolve to their base family. Keep this in sync with
# https://openai.com/api/pricing/ ; for any model not listed, the operator can
# set LLM_PRICE_INPUT_PER_1M / LLM_PRICE_OUTPUT_PER_1M env vars as a fallback.
MODEL_PRICING = {
    "gpt-4-turbo": (10.00, 30.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4-32k": (60.00, 120.00),
    "gpt-4": (30.00, 60.00),
    "gpt-3.5-turbo": (0.50, 1.50),
    "o1-mini": (3.00, 12.00),
    "o1": (15.00, 60.00),
    "o3-mini": (1.10, 4.40),
}


class LLMAssistant:
    """LLM-powered document analysis and Q&A"""
    
    SYSTEM_PROMPT = """You are an expert research assistant specializing in analyzing legal documents, 
court records, and investigative files related to the Jeffrey Epstein cases. 

Your role is to:
1. Answer questions based on the provided document excerpts
2. Identify key facts, names, dates, and connections
3. Summarize complex legal language in plain terms
4. Cross-reference information across multiple documents when relevant
5. Clearly distinguish between what is stated in documents vs. interpretation

Important guidelines:
- Only make claims that are directly supported by the provided documents
- If information is not in the provided context, say so
- Be objective and factual - avoid speculation
- Note when documents are redacted or incomplete
- Cite specific documents when making claims

The documents you analyze include court filings, flight logs, contact books, 
DOJ reports, and other official records."""

    def __init__(self, api_key: Optional[str] = None, model: str = "gpt-4-turbo",
                 usage_recorder: Optional[Callable[..., None]] = None):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        # Allow the deployed model to be overridden without a code change.
        self.model = os.getenv("OPENAI_MODEL", model)
        # Optional callback(operation, model, prompt_tokens, completion_tokens, cost_usd)
        # invoked after every API call so cost can be persisted for the admin dashboard.
        self.usage_recorder = usage_recorder
        self.client = None

        if self.api_key:
            self.client = OpenAI(api_key=self.api_key)

    def is_available(self) -> bool:
        """Check if LLM is configured and available"""
        return self.client is not None

    def _price_for_model(self, model: str) -> tuple:
        """Return (input_per_1m, output_per_1m) USD pricing for a model.

        Uses longest-prefix matching against MODEL_PRICING, then falls back to
        operator-provided env vars, then to (0, 0) if the model is unknown.
        """
        best_price = None
        best_len = -1
        for prefix, price in MODEL_PRICING.items():
            if model.startswith(prefix) and len(prefix) > best_len:
                best_price, best_len = price, len(prefix)
        if best_price is not None:
            return best_price
        inp = os.getenv("LLM_PRICE_INPUT_PER_1M")
        out = os.getenv("LLM_PRICE_OUTPUT_PER_1M")
        if inp and out:
            try:
                return (float(inp), float(out))
            except ValueError:
                pass
        return (0.0, 0.0)

    def _record_usage(self, operation: str, usage: Any) -> None:
        """Compute cost from an OpenAI `usage` object and hand it to the recorder.

        Best-effort: swallows all errors so usage accounting never breaks the
        AI response the user is waiting on.
        """
        if not self.usage_recorder or usage is None:
            return
        try:
            prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
            in_price, out_price = self._price_for_model(self.model)
            cost_usd = (prompt_tokens / 1_000_000) * in_price + \
                       (completion_tokens / 1_000_000) * out_price
            self.usage_recorder(
                operation=operation,
                model=self.model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_usd=cost_usd,
            )
        except Exception as e:
            print(f"⚠ LLM usage recording failed: {e}")
    
    def format_context(self, documents: List[Dict[str, Any]], max_chars: int = 24000) -> str:
        """Format documents as context for LLM
        
        Args:
            documents: List of documents with 'full_text' or 'text' fields
            max_chars: Maximum total characters for context (increased for better accuracy)
        """
        context_parts = []
        total_chars = 0
        
        # Sort by relevance score to prioritize most relevant docs
        sorted_docs = sorted(documents, key=lambda d: d.get('score', 0), reverse=True)
        
        for doc in sorted_docs:
            # Prefer full_text over the shorter text snippet
            content = doc.get('full_text', doc.get('text', 'No content available'))
            
            # Use up to 5000 chars per document for better context
            content_truncated = content[:5000] if len(content) > 5000 else content
            
            doc_text = f"""
---
Document: {doc.get('filename', 'Unknown')}
Category: {doc.get('category', 'Unknown')}
{f"Subcategory: {doc.get('subcategory')}" if doc.get('subcategory') else ""}
Relevance Score: {doc.get('score', 'N/A'):.3f if isinstance(doc.get('score'), float) else 'N/A'}

Content:
{content_truncated}
---
"""
            if total_chars + len(doc_text) > max_chars:
                # Try to include at least a shorter version
                remaining = max_chars - total_chars - 200
                if remaining > 500:
                    short_content = content[:remaining]
                    short_doc = f"""
---
Document: {doc.get('filename', 'Unknown')}
Category: {doc.get('category', 'Unknown')}

Content (truncated):
{short_content}...
---
"""
                    context_parts.append(short_doc)
                break
            context_parts.append(doc_text)
            total_chars += len(doc_text)
        
        return "\n".join(context_parts)
    
    def answer_question(self, question: str, context_documents: List[Dict[str, Any]], 
                        stream: bool = False) -> str:
        """Answer a question based on document context"""
        if not self.is_available():
            return "LLM is not configured. Please set OPENAI_API_KEY environment variable."
        
        context = self.format_context(context_documents)
        
        if not context.strip():
            return "No relevant documents found to answer this question."
        
        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": f"""Based on the following document excerpts, please answer this question:

Question: {question}

Document Context:
{context}

Please provide a detailed, factual answer based only on the information in these documents. 
Cite specific documents when making claims. If the documents don't contain enough information 
to fully answer the question, explain what information is available and what is missing."""}
        ]
        
        try:
            if stream:
                return self._stream_response(messages)
            else:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=0.3,
                    max_tokens=2000
                )
                self._record_usage("ask", getattr(response, "usage", None))
                return response.choices[0].message.content
        except Exception:
            return "An error occurred while generating the response. Please try again."

    def _stream_response(self, messages: List[Dict[str, str]], operation: str = "ask_stream"):
        """Stream response from LLM.

        Requests token usage in the final stream chunk (stream_options) so cost
        can be recorded; falls back gracefully on SDK versions that lack it.
        """
        try:
            stream = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.3,
                max_tokens=2000,
                stream=True,
                stream_options={"include_usage": True},
            )
        except TypeError:
            # Older openai SDK without stream_options support.
            stream = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.3,
                max_tokens=2000,
                stream=True,
            )

        usage = None
        for chunk in stream:
            # The usage-bearing final chunk carries no choices, so guard access.
            if getattr(chunk, "usage", None):
                usage = chunk.usage
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

        self._record_usage(operation, usage)
    
    def summarize_document(self, document: Dict[str, Any]) -> str:
        """Generate a summary of a single document"""
        if not self.is_available():
            return "LLM is not configured."
        
        text = document.get("full_text", "")[:6000]
        
        if not text.strip():
            return "No content available to summarize."
        
        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": f"""Please provide a concise summary of this document:

Filename: {document.get('filename', 'Unknown')}
Category: {document.get('category', 'Unknown')}

Document Content:
{text}

Please summarize:
1. What type of document this is
2. Key facts, names, dates mentioned
3. Main points or significance
4. Any notable redactions or missing information"""}
        ]
        
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.3,
                max_tokens=1000
            )
            self._record_usage("summary", getattr(response, "usage", None))
            return response.choices[0].message.content
        except Exception:
            return "An error occurred while generating the summary. Please try again."
    
    def extract_entities(self, text: str) -> Dict[str, List[str]]:
        """Extract named entities from text"""
        if not self.is_available():
            return {"error": "LLM is not configured."}
        
        messages = [
            {"role": "system", "content": "You are an entity extraction system. Extract and categorize entities from the provided text. Return JSON only."},
            {"role": "user", "content": f"""Extract named entities from this text and return as JSON with these categories:
- people: List of person names
- organizations: List of organization names
- locations: List of locations/addresses
- dates: List of dates mentioned
- phone_numbers: List of phone numbers
- other: Any other notable entities

Text:
{text[:4000]}

Return valid JSON only, no other text."""}
        ]
        
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0,
                max_tokens=1000,
                response_format={"type": "json_object"}
            )
            self._record_usage("extract_entities", getattr(response, "usage", None))
            import json
            return json.loads(response.choices[0].message.content)
        except Exception:
            return {"error": "Failed to extract entities"}


def fetch_provider_balance() -> Dict[str, Any]:
    """Best-effort fetch of the OpenAI account's remaining credit balance.

    OpenAI does NOT expose remaining prepaid balance to standard `sk-` API keys;
    the legacy dashboard endpoint used here needs a browser *session* key
    (`sess-...`) which the operator can supply via OPENAI_BILLING_KEY. When that
    isn't available (the common case) this returns a non-'ok' status and the UI
    falls back to the snapshot-anchored balance. Never raises.

    Returns a dict with a "status" of:
      - "ok"          + total_granted / total_used / total_available
      - "unconfigured"  (no key set)
      - "unavailable"   (key rejected — expected for standard API keys)
      - "error"         (network / unexpected response)
    """
    key = os.getenv("OPENAI_BILLING_KEY") or os.getenv("OPENAI_API_KEY")
    if not key:
        return {"status": "unconfigured",
                "reason": "Set OPENAI_BILLING_KEY (a dashboard session key) to fetch live balance."}

    url = "https://api.openai.com/dashboard/billing/credit_grants"
    try:
        import httpx
        resp = httpx.get(url, headers={"Authorization": f"Bearer {key}"}, timeout=10.0)
    except Exception as e:
        return {"status": "error", "reason": f"Request to provider failed ({e.__class__.__name__})."}

    if resp.status_code == 200:
        try:
            d = resp.json()
        except Exception:
            return {"status": "error", "reason": "Provider returned an unreadable response."}
        return {
            "status": "ok",
            "total_granted": d.get("total_granted"),
            "total_used": d.get("total_used"),
            "total_available": d.get("total_available"),
            "currency": "usd",
        }
    if resp.status_code in (401, 403):
        return {"status": "unavailable",
                "reason": ("OpenAI does not expose credit balance to standard API keys. "
                           "This needs a dashboard session key (OPENAI_BILLING_KEY); "
                           "otherwise rely on the balance snapshot below.")}
    return {"status": "error", "reason": f"Provider returned HTTP {resp.status_code}."}

