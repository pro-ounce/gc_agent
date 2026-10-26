#!/usr/bin/env python3
"""
recover-from-snapshot.py — recover agent-kv records (chat history, config, tools) from an
OpenSearch snapshot WITHOUT disturbing live data. This is the supported "recover from data loss"
path the daily snapshots exist for (SM policy `agent-daily`, repo `agent-fs`, 14-day retention).

Run on the box with the agent venv + sudo (the OpenSearch creds live in the 0640 supervisor conf):

    sudo /apps/gc_agent/bin/python /apps/gc_agent/deploy/recover-from-snapshot.py list
    sudo .../recover-from-snapshot.py recover --snapshot <name> --key history:chatbot:1        # DRY RUN
    sudo .../recover-from-snapshot.py recover --snapshot <name> --key history:chatbot:1 --write # apply
    sudo .../recover-from-snapshot.py recover --snapshot <name> --key-prefix history: --write    # many
        [--app formulation]   # re-tag recovered conversation(s) to chatbot:<user>:<app> (per-app history)

How it works (safe by construction):
  1. restore the snapshot's `agent-kv` into a TEMP index `agent-kv-recover` (rename on restore — the
     live index is never touched);
  2. read the requested doc(s) from the temp index;
  3. re-index each into live `agent-kv` under a NEW immutable archive key `history:<sid>[:app]:<ms>`
     (value stored as a JSON STRING — the `value` field is mapped text; a dict → mapper error),
     copying `kind` from an existing live history doc; NO existing doc is overwritten;
  4. delete the temp index.
Default is a DRY RUN (prints what it would write); pass --write to apply.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time

CONF = "/etc/supervisor/conf.d/ai-agent-service.conf"
REPO = "agent-fs"
KV_INDEX = "agent-kv"
TMP_INDEX = "agent-kv-recover"


def _conf_val(txt: str, key: str) -> str | None:
    m = re.search(key + r'="([^"]*)"', txt) or re.search(key + r"=([^,\n]+)", txt)
    return m.group(1).strip() if m else None


def _client():
    try:
        txt = open(CONF).read()
    except PermissionError:
        sys.exit(f"ERROR: cannot read {CONF} — run under sudo.")
    from opensearchpy import OpenSearch
    url = _conf_val(txt, "OPENSEARCH_URL")
    return OpenSearch(
        [url],
        http_auth=(_conf_val(txt, "OPENSEARCH_USERNAME"), _conf_val(txt, "OPENSEARCH_PASSWORD")),
        verify_certs=str(_conf_val(txt, "OPENSEARCH_VERIFY_CERTS")).lower() == "true",
        ssl_show_warn=False,
    )


def cmd_list(c, args):
    snaps = (c.snapshot.get(repository=REPO, snapshot="_all", ignore=[404]) or {}).get("snapshots", [])
    print(f"{len(snaps)} snapshots in repo '{REPO}' (newest last):")
    for s in snaps[-args.limit:]:
        print(f"  {s.get('snapshot'):52}  {str(s.get('start_time'))[:16]}  {s.get('state')}")


def _get_value(src: dict) -> dict:
    v = src.get("value")
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            v = {}
    return v if isinstance(v, dict) else {}


def cmd_recover(c, args):
    # 1) restore into a temp index (live agent-kv untouched)
    c.indices.delete(index=TMP_INDEX, ignore=[404])
    print(f"restoring {args.snapshot} → {TMP_INDEX} …")
    c.snapshot.restore(
        repository=REPO, snapshot=args.snapshot, wait_for_completion=True,
        body={"indices": KV_INDEX, "rename_pattern": KV_INDEX,
              "rename_replacement": TMP_INDEX, "include_aliases": False},
    )
    try:
        # 2) collect the requested docs
        docs: list[tuple[str, dict]] = []
        if args.key:
            d = c.get(index=TMP_INDEX, id=args.key, ignore=[404])
            if d.get("found"):
                docs.append((args.key, _get_value(d["_source"])))
        if args.key_prefix:
            q = {"query": {"prefix": {"_id": args.key_prefix}}, "size": 1000}
            # _id prefix isn't queryable; scan all and filter client-side instead
            scan = c.search(index=TMP_INDEX, body={"query": {"match_all": {}}, "size": 5000})
            for h in scan.get("hits", {}).get("hits", []):
                if str(h.get("_id", "")).startswith(args.key_prefix):
                    docs.append((h["_id"], _get_value(h["_source"])))
        if not docs:
            print("nothing matched — no docs recovered.")
            return
        # kind to mirror from an existing live history doc (fallback to any live doc)
        kind = None
        for probe in ("history:chatbot:1", None):
            try:
                ref = c.get(index=KV_INDEX, id=probe)["_source"] if probe else None
                if ref:
                    kind = ref.get("kind")
                    break
            except Exception:
                pass
        for src_key, v in docs:
            if not v.get("messages"):
                print(f"  skip {src_key}: no messages")
                continue
            sid = v.get("session_id") or src_key.split("#")[0].replace("history:", "")
            if args.app:
                base = ":".join(sid.split(":")[:2])  # agent:user
                sid = f"{base}:{args.app}"
                v["session_id"] = sid
            new_key = f"history:{sid}:{int(time.time() * 1000)}"
            days = sorted({(m.get("timestamp") or "")[:10] for m in v["messages"] if isinstance(m, dict)})
            print(f"  {'WRITE' if args.write else 'DRY '} {new_key}  msgs={len(v['messages'])}  days={days}")
            if args.write:
                c.index(index=KV_INDEX, id=new_key,
                        body={"value": json.dumps(v), "kind": kind, "expires_at": None}, refresh=True)
                time.sleep(0.001)
        if not args.write:
            print("\n(dry run — pass --write to apply)")
    finally:
        c.indices.delete(index=TMP_INDEX, ignore=[404])
        print(f"temp index {TMP_INDEX} cleaned up.")


def main():
    ap = argparse.ArgumentParser(description="Recover agent-kv records from an OpenSearch snapshot.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("list", help="list snapshots in the repo")
    pl.add_argument("--limit", type=int, default=20)
    pr = sub.add_parser("recover", help="recover doc(s) from a snapshot into live agent-kv")
    pr.add_argument("--snapshot", required=True, help="snapshot name (see `list`)")
    pr.add_argument("--key", default="", help="exact doc key, e.g. history:chatbot:1")
    pr.add_argument("--key-prefix", default="", help="recover every doc whose id starts with this")
    pr.add_argument("--app", default="", help="re-tag recovered convo(s) to chatbot:<user>:<app>")
    pr.add_argument("--write", action="store_true", help="actually write (default: dry run)")
    args = ap.parse_args()
    c = _client()
    (cmd_list if args.cmd == "list" else cmd_recover)(c, args)


if __name__ == "__main__":
    main()
