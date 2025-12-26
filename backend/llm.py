"""
LLM Integration Module
Provides intelligent Q&A and analysis over documents
"""

import os
from typing import List, Dict, Any, Optional
from openai import OpenAI


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

    def __init__(self, api_key: Optional[str] = None, model: str = "gpt-4-turbo"):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.model = model
        self.client = None
        
        if self.api_key:
            self.client = OpenAI(api_key=self.api_key)
    
    def is_available(self) -> bool:
        """Check if LLM is configured and available"""
        return self.client is not None
    
    def format_context(self, documents: List[Dict[str, Any]], max_chars: int = 12000) -> str:
        """Format documents as context for LLM"""
        context_parts = []
        total_chars = 0
        
        for doc in documents:
            doc_text = f"""
---
Document: {doc.get('filename', 'Unknown')}
Category: {doc.get('category', 'Unknown')} / {doc.get('subcategory', '')}
Relevance Score: {doc.get('score', 'N/A')}

Content:
{doc.get('text', doc.get('full_text', 'No content available'))[:3000]}
---
"""
            if total_chars + len(doc_text) > max_chars:
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
                return response.choices[0].message.content
        except Exception:
            return "An error occurred while generating the response. Please try again."
    
    def _stream_response(self, messages: List[Dict[str, str]]):
        """Stream response from LLM"""
        stream = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=0.3,
            max_tokens=2000,
            stream=True
        )
        
        for chunk in stream:
            if chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
    
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
            import json
            return json.loads(response.choices[0].message.content)
        except Exception:
            return {"error": "Failed to extract entities"}

