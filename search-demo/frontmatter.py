#!/usr/bin/env python3
"""Dependent-free YAML frontmatter parsing, shared by index_qdrant.py and
skills_map.py so neither has to duplicate it or drag in the other's deps."""

import re


def parse_frontmatter(text: str) -> dict:
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not match:
        return {}
    fields = {}
    lines = match.group(1).splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if ":" not in line or line.startswith((" ", "\t")):
            i += 1
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value in ("|", ">", "|-", ">-", "|+", ">+"):
            # YAML block scalar: value is the indented lines that follow.
            block = []
            i += 1
            while i < len(lines) and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
                block.append(lines[i])
                i += 1
            # Fold ">" style like YAML does; keep "|" style line breaks as-is.
            dedented = "\n".join(l.lstrip() for l in block).strip()
            fields[key] = dedented if value.startswith("|") else " ".join(dedented.splitlines())
            continue
        fields[key] = value.strip('"').strip("'")
        i += 1
    return fields
