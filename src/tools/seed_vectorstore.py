"""
Seed the Chroma vector store with sample historical incidents.

Run with:  python -m src.tools.seed_vectorstore
Or:        make seed
"""
from __future__ import annotations

import json
from pathlib import Path

from src.tools.vectorstore import add_incidents, get_collection


def main() -> None:
    seed_path = Path("data/seed_incidents.jsonl")
    if not seed_path.exists():
        print(f"Seed file not found: {seed_path}")
        return

    incidents = []
    with seed_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            incidents.append(json.loads(line))

    add_incidents(incidents)
    coll = get_collection()
    print(f"Loaded {len(incidents)} incidents. Collection now has {coll.count()} entries.")


if __name__ == "__main__":
    main()
