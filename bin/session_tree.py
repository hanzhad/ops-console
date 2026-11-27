"""Session tree linking: parent, root and tree totals for a list of sessions."""

from typing import Any, Dict, List


def link_session_tree(sessions: List[Dict[str, Any]],
                      lookup: Dict[str, Dict[str, Any]],
                      default_limit: int = 1) -> None:
    """Give each session its parent and its tree's root, in place.

    parent_id is normalised to the parent's own id, and dropped when the parent is
    not among the sessions (or would make a cycle). root_id/root_title name the
    top of the tree. On a root, tree_held counts the slots held by the whole tree
    (purely informational, never enforced).
    """
    by_id = {s["id"]: s for s in sessions if s.get("id")}
    for s in sessions:
        p = s.get("parent_id")
        if p:
            parent = lookup.get(p) or lookup.get(p.lower())
            pid = parent.get("id") if parent else None
            s["parent_id"] = pid if pid in by_id and pid != s.get("id") else None
        else:
            s["parent_id"] = None

    for s in sessions:
        seen = {s.get("id")}
        node = s
        while node.get("parent_id") and node["parent_id"] not in seen:
            seen.add(node["parent_id"])
            node = by_id[node["parent_id"]]
        if node.get("parent_id"):  # cycle: cut it here
            s["parent_id"] = None
            node = s
        s["root_id"] = node.get("id")
        s["root_title"] = node.get("title")

    for s in sessions:
        s["tree_held"] = 0
        s["tree_over_limit"] = False
    for s in sessions:
        root = by_id.get(s.get("root_id"))
        if root is None:
            continue
        root["tree_held"] += s.get("held_slots", 0)
        eff = s["limit"] if s.get("limit") is not None else default_limit
        if s.get("is_over_limit") or s.get("held_slots", 0) > eff:
            root["tree_over_limit"] = True
