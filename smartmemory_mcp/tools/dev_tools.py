"""Development workflow MCP tools (6 tools -- dev_link_commit and dev_roadmap_status dropped)."""

import logging
from datetime import datetime, timezone
from typing import List, Optional

from .common import get_backend, graceful

logger = logging.getLogger(__name__)


# Valid friction categories and severities
FRICTION_CATEGORIES = (
    "tool_failure",
    "confusing_api",
    "missing_feature",
    "slow_response",
    "workaround_needed",
)
FRICTION_SEVERITIES = ("low", "medium", "high")


def register(mcp):
    """Register development workflow tools with the MCP server."""

    @mcp.tool()
    @graceful
    def dev_record_decision(
        title: str,
        context: str,
        decision: str,
        rationale: str,
        alternatives: Optional[str] = None,
        feature_id: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> str:
        """Record an architectural or design decision as a decision memory."""

        backend = get_backend()

        content = f"Decision: {title}\n\nContext: {context}\n\nDecision: {decision}\n\nRationale: {rationale}"
        if alternatives:
            content += f"\n\nAlternatives considered: {alternatives}"

        item_metadata = {
            "entity_type": "decision",
            "title": title,
            "context": context,
            "rationale": rationale,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        if alternatives:
            item_metadata["alternatives"] = alternatives
        if feature_id:
            item_metadata["feature_id"] = feature_id

        all_tags = list(tags) if tags else []
        all_tags.append("dev-decision")
        if feature_id:
            all_tags.append(feature_id)
        item_metadata["tags"] = all_tags

        item_id = backend.add(content, memory_type="decision", metadata=item_metadata)

        return (
            f"Decision recorded: {item_id}\n"
            f"Title: {title}\n"
            f"Feature: {feature_id or 'none'}\n"
            f"Search tip: use dev_query_decisions with query related to '{title}'"
        )

    @mcp.tool()
    @graceful
    def dev_query_decisions(
        query: str,
        feature_id: Optional[str] = None,
        since: Optional[str] = None,
        limit: int = 5,
    ) -> str:
        """Search past development decisions by keyword, feature, or time range."""
        backend = get_backend()
        results = backend.search(query, top_k=limit * 2, memory_type="decision")

        if not results:
            return f"No decisions found for query: {query}"

        filtered = []
        for item in results:
            meta = item["metadata"]

            if feature_id and meta.get("feature_id") != feature_id:
                continue

            if since:
                recorded_at = meta.get("recorded_at", "")
                if recorded_at:
                    try:
                        rec_dt = datetime.fromisoformat(recorded_at)
                        since_dt = datetime.fromisoformat(since)
                        if rec_dt.tzinfo is None:
                            rec_dt = rec_dt.replace(tzinfo=timezone.utc)
                        if since_dt.tzinfo is None:
                            since_dt = since_dt.replace(tzinfo=timezone.utc)
                        if rec_dt < since_dt:
                            continue
                    except (ValueError, TypeError):
                        pass

            filtered.append(item)
            if len(filtered) >= limit:
                break

        if not filtered:
            filters = []
            if feature_id:
                filters.append(f"feature_id={feature_id}")
            if since:
                filters.append(f"since={since}")
            return (
                f"No decisions found for query: {query} (filters: {', '.join(filters)})"
            )

        output = [f"Found {len(filtered)} decisions for '{query}':\n"]
        for i, item in enumerate(filtered, 1):
            meta = item["metadata"]
            title = meta.get("title", "Untitled")
            rationale = meta.get("rationale", "")
            recorded_at = meta.get("recorded_at", "unknown")
            fid = meta.get("feature_id", "")

            output.append(f"{i}. [{item['item_id']}] {title}")
            if fid:
                output.append(f"   Feature: {fid}")
            output.append(f"   Date: {recorded_at}")
            if rationale:
                preview = rationale[:150] + "..." if len(rationale) > 150 else rationale
                output.append(f"   Rationale: {preview}")
            output.append("")

        return "\n".join(output)

    @mcp.tool()
    @graceful
    def dev_save_session(
        summary: str,
        key_findings: Optional[List[str]] = None,
        files_modified: Optional[List[str]] = None,
        next_steps: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
    ) -> str:
        """Save a development session summary as an episodic memory."""

        backend = get_backend()

        parts = [f"Session Summary: {summary}"]
        if key_findings:
            parts.append("Key Findings: " + "; ".join(key_findings))
        if files_modified:
            parts.append("Files Modified: " + ", ".join(files_modified))
        if next_steps:
            parts.append("Next Steps: " + "; ".join(next_steps))

        content = "\n".join(parts)

        item_metadata = {
            "entity_type": "session",
            "summary": summary,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        if key_findings:
            item_metadata["key_findings"] = key_findings
        if files_modified:
            item_metadata["files_modified"] = files_modified
        if next_steps:
            item_metadata["next_steps"] = next_steps

        all_tags = list(tags) if tags else []
        all_tags.append("dev-session")
        item_metadata["tags"] = all_tags

        result = backend.ingest(content, memory_type="episodic")
        if isinstance(result, dict):
            session_id = result.get("item_id", str(result))
        else:
            session_id = str(result)

        return f"Session saved: {session_id}\nSummary: {summary}"

    @mcp.tool()
    @graceful
    def dev_load_context(
        topic: str,
        sessions: int = 3,
        include_decisions: bool = True,
    ) -> str:
        """Load recent session context and related decisions for a work topic."""
        backend = get_backend()

        output = []

        session_results = backend.search(topic, top_k=sessions, memory_type="episodic")
        if session_results:
            output.append(f"=== Recent Sessions ({len(session_results)}) ===\n")
            for i, item in enumerate(session_results, 1):
                meta = item["metadata"]
                summary = meta.get("summary", str(item["content"])[:200])
                recorded_at = meta.get("recorded_at", "unknown")
                next_steps = meta.get("next_steps", [])

                output.append(f"{i}. [{recorded_at}] {summary}")
                if next_steps:
                    output.append(f"   Next steps: {'; '.join(next_steps)}")
                output.append("")
        else:
            output.append("No recent sessions found.\n")

        decision_results = []
        if include_decisions:
            decision_results = (
                backend.search(topic, top_k=3, memory_type="decision") or []
            )
            if decision_results:
                output.append(f"=== Related Decisions ({len(decision_results)}) ===\n")
                for i, item in enumerate(decision_results, 1):
                    meta = item["metadata"]
                    title = meta.get("title", "Untitled")
                    rationale = meta.get("rationale", "")
                    preview = (
                        rationale[:100] + "..." if len(rationale) > 100 else rationale
                    )

                    output.append(f"{i}. {title}")
                    if preview:
                        output.append(f"   Rationale: {preview}")
                    output.append("")
            else:
                output.append("No related decisions found.\n")

        context_parts = []
        if session_results:
            latest = session_results[0]
            latest_meta = latest["metadata"]
            context_parts.append(f"Last session: {latest_meta.get('summary', 'N/A')}")
            ns = latest_meta.get("next_steps", [])
            if ns:
                context_parts.append(f"Pending: {'; '.join(ns)}")
        if include_decisions and decision_results:
            titles = [d["metadata"].get("title", "?") for d in decision_results[:3]]
            context_parts.append(f"Relevant decisions: {', '.join(titles)}")

        if context_parts:
            output.append("=== Suggested Context ===\n")
            output.append(". ".join(context_parts))

        return "\n".join(output) if output else f"No context found for topic: {topic}"

    @mcp.tool()
    @graceful
    def dev_record_pattern(
        name: str,
        description: str,
        example: Optional[str] = None,
        applies_to: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> str:
        """Record a code pattern or convention as procedural memory."""

        backend = get_backend()

        parts = [f"Pattern: {name}\n\n{description}"]
        if example:
            parts.append(f"\nExample:\n{example}")
        if applies_to:
            parts.append(f"\nApplies to: {applies_to}")

        content = "\n".join(parts)

        item_metadata = {
            "entity_type": "pattern",
            "name": name,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        if applies_to:
            item_metadata["applies_to"] = applies_to

        all_tags = list(tags) if tags else []
        all_tags.append("dev-pattern")
        item_metadata["tags"] = all_tags

        pattern_id = backend.add(
            content, memory_type="procedural", metadata=item_metadata
        )

        return f"Pattern recorded: {pattern_id}\nName: {name}"

    @mcp.tool()
    @graceful
    def dev_log_friction(
        description: str,
        category: str,
        severity: str = "medium",
        context: Optional[str] = None,
    ) -> str:
        """Log a friction event encountered during development."""

        if category not in FRICTION_CATEGORIES:
            return f"Invalid category: {category}. Must be one of: {', '.join(FRICTION_CATEGORIES)}"

        if severity not in FRICTION_SEVERITIES:
            return f"Invalid severity: {severity}. Must be one of: {', '.join(FRICTION_SEVERITIES)}"

        backend = get_backend()

        parts = [
            f"Friction: {description}",
            f"Category: {category}",
            f"Severity: {severity}",
        ]
        if context:
            parts.append(f"Context: {context}")
        content = "\n".join(parts)

        item_metadata = {
            "entity_type": "friction",
            "source": "friction-log",
            "category": category,
            "severity": severity,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "tags": ["dev-friction", f"friction-{category}", f"severity-{severity}"],
        }
        if context:
            item_metadata["context"] = context

        result = backend.ingest(content, memory_type="observation")
        if isinstance(result, dict):
            friction_id = result.get("item_id", str(result))
        else:
            friction_id = str(result)

        return (
            f"Friction logged: {friction_id}\n"
            f"Category: {category}\n"
            f"Severity: {severity}\n"
            f"Tip: search for friction with memory_search query='{description[:50]}'"
        )
