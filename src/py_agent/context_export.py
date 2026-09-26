"""Standalone, lossless HTML export for inspecting a session context snapshot."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
import json
import os
from pathlib import Path
from typing import Any


def _json_for_script(value: Mapping[str, Any]) -> str:
    """Serialize JSON without allowing data to terminate the containing script tag."""
    return (
        json
        .dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def render_context_html(payload: Mapping[str, Any]) -> str:
    """Render a self-contained context explorer; untrusted values remain text nodes."""
    data = _json_for_script(payload)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark light">
<title>py context snapshot</title>
<style>
:root{{--bg:#0b1020;--panel:#121a2d;--panel2:#18233b;--text:#e8edf7;--muted:#98a6bd;--line:#2a3855;--accent:#7dd3fc;--system:#c4b5fd;--user:#86efac;--assistant:#fcd34d;--observation:#fda4af}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 ui-sans-serif,system-ui,sans-serif}} button,input{{font:inherit}} header{{position:sticky;top:0;z-index:2;padding:18px max(20px,calc((100vw - 1180px)/2));background:#0b1020ee;border-bottom:1px solid var(--line);backdrop-filter:blur(10px)}}
h1{{font-size:20px;margin:0 0 10px}} .controls{{display:flex;gap:8px;flex-wrap:wrap}} input{{flex:1;min-width:220px;color:var(--text);background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:9px 12px}} button{{color:var(--text);background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:7px 10px;cursor:pointer}} button:hover{{border-color:var(--accent)}}
main{{max-width:1180px;margin:auto;padding:22px}} .summary{{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:10px;margin-bottom:18px}} .stat,.section,.message{{background:var(--panel);border:1px solid var(--line);border-radius:10px}} .stat{{padding:12px}} .stat b{{display:block;font-size:18px}} .stat span,.muted{{color:var(--muted)}}
.section{{margin:14px 0;overflow:hidden}} .section>summary,.message>summary{{cursor:pointer;padding:12px 14px;font-weight:650}} .section-body{{padding:0 14px 14px}} .messages{{display:grid;gap:10px}} .message{{background:var(--panel2)}} .message[hidden]{{display:none}} .message>summary{{display:flex;gap:10px;align-items:center}} .role{{font-size:11px;text-transform:uppercase;letter-spacing:.08em;padding:2px 7px;border:1px solid currentColor;border-radius:999px}} .system{{color:var(--system)}} .user{{color:var(--user)}} .assistant{{color:var(--assistant)}} .observation{{color:var(--observation)}} .phase{{color:var(--muted);font-weight:400}}
pre{{margin:0;padding:12px 14px;border-top:1px solid var(--line);white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.55 ui-monospace,SFMono-Regular,Consolas,monospace}} .json{{max-height:65vh;overflow:auto}} .copy{{float:right;margin:7px}} .empty{{padding:14px;color:var(--muted)}} footer{{color:var(--muted);padding:18px 0}} mark{{background:#854d0e;color:white}}
</style>
</head>
<body>
<header><h1>py context snapshot</h1><div class="controls"><input id="search" type="search" placeholder="Search all messages…" autofocus><button id="expand">Expand all</button><button id="collapse">Collapse all</button></div></header>
<main><div id="summary" class="summary"></div><div id="content"></div><footer>This file may contain prompts, source, output, paths, and secrets. Keep it private.</footer></main>
<script id="snapshot" type="application/json">{data}</script>
<script>
const data=JSON.parse(document.getElementById('snapshot').textContent);const root=document.getElementById('content');
const esc=s=>String(s??'');
function stat(label,value){{const d=document.createElement('div');d.className='stat';const b=document.createElement('b');b.textContent=value;const s=document.createElement('span');s.textContent=label;d.append(b,s);return d}}
const current=data.current_context||{{messages:[]}};document.getElementById('summary').append(stat('messages',current.messages?.length||0),stat('epoch',current.epoch??'?'),stat('last model request',data.last_model_request?'yes':'none'),stat('collapsed archives',data.collapsed_archives?.length||0));
function copyButton(text){{const b=document.createElement('button');b.className='copy';b.textContent='Copy';b.onclick=async e=>{{e.preventDefault();try{{if(navigator.clipboard)await navigator.clipboard.writeText(text);else throw Error()}}catch{{const t=document.createElement('textarea');t.value=text;document.body.append(t);t.select();document.execCommand('copy');t.remove()}}b.textContent='Copied';setTimeout(()=>b.textContent='Copy',900)}};return b}}
function messageCard(m,i){{const d=document.createElement('details');d.className='message';d.open=i===0||i>=(current.messages?.length||0)-2;d.dataset.search=(esc(m.role)+' '+esc(m.phase)+' '+esc(m.content)).toLowerCase();const s=document.createElement('summary');const r=document.createElement('span');r.className='role '+m.role;r.textContent=m.role;const n=document.createTextNode(' #'+(i+1));s.append(r,n);if(m.phase){{const p=document.createElement('span');p.className='phase';p.textContent=m.phase;s.append(p)}}const pre=document.createElement('pre');pre.textContent=esc(m.content);d.append(s,copyButton(esc(m.content)),pre);return d}}
function section(title,open=true){{const d=document.createElement('details');d.className='section';d.open=open;const s=document.createElement('summary');s.textContent=title;const b=document.createElement('div');b.className='section-body';d.append(s,b);root.append(d);return b}}
const mb=section('Current live context',true);const ml=document.createElement('div');ml.className='messages';(current.messages||[]).forEach((m,i)=>ml.append(messageCard(m,i)));if(!ml.children.length)ml.innerHTML='<div class="empty">No messages.</div>';mb.append(ml);
const exact=data.last_model_request?.context?.messages||[];if(exact.length){{const eb=section('Last exact dispatched context',false);const el=document.createElement('div');el.className='messages';exact.forEach((m,i)=>el.append(messageCard(m,i)));eb.append(el)}}
function jsonSection(title,value,open=false){{const b=section(title,open);const text=JSON.stringify(value,null,2);b.append(copyButton(text));const p=document.createElement('pre');p.className='json';p.textContent=text;b.append(p)}}
jsonSection('Last exact dispatched model request',data.last_model_request);jsonSection('Runtime, commands, and model tools',data.runtime);jsonSection('Raw context structure',data.raw_context);jsonSection('Collapsed context archives',data.collapsed_archives);jsonSection('Export metadata',data.export);
document.getElementById('search').addEventListener('input',e=>{{const q=e.target.value.toLowerCase();document.querySelectorAll('.message').forEach(x=>x.hidden=q&&!x.dataset.search.includes(q))}});document.getElementById('expand').onclick=()=>document.querySelectorAll('details').forEach(x=>x.open=true);document.getElementById('collapse').onclick=()=>document.querySelectorAll('details').forEach(x=>x.open=false);
</script>
</body></html>"""


def default_export_path(session_id: str, directory: Path | None = None) -> Path:
    """Choose a non-existing, recognizable filename in the selected directory."""
    parent = Path.cwd() if directory is None else directory
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%SZ")
    stem = f"py-context-{stamp}-{session_id[:8]}"
    candidate = parent / f"{stem}.html"
    index = 2
    while candidate.exists():
        candidate = parent / f"{stem}-{index}.html"
        index += 1
    return candidate


def write_context_html(path: Path, payload: Mapping[str, Any]) -> Path:
    """Create a private snapshot without following or overwriting an existing path."""
    target = path.expanduser().absolute()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = -1
            handle.write(render_context_html(payload))
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            target.unlink()
        except OSError:
            pass
        raise
    return target
