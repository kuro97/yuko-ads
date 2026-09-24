"""Local-only browser fixture for launch queue DOM interaction checks."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "web" / "static" / "index.html"

MOCK_SCRIPT = r"""
<script>
localStorage.setItem('api_key', 'browser-local-key');
window.__launchRequests = [];
window.__lastLaunchCard = null;
document.addEventListener('DOMContentLoaded', () => {
  const proof = document.createElement('pre');
  proof.id = 'browser-request-proof';
  proof.hidden = true;
  proof.textContent = '[]';
  document.body.appendChild(proof);
});
window.__cardsPayload = {
  raw_count: 6,
  eligible_count: 0,
  preflight_pending_count: 5,
  blocked_count: 1,
  cards: [
    {id:'card-6',name:'Карточка 6',desc:'',pos:60,language:'L1',media_type:'image',labels:[],product:null,format_labels:[],launch_status:'needs_full_preflight',reason_codes:[],reasons:[]},
    {id:'card-3',name:'Карточка бонус 3',desc:'',pos:30,language:'L1',media_type:'image',labels:['PRODB'],product:'PRODB',format_labels:[],launch_status:'blocked',reason_codes:['TOPIC_VETO'],reasons:['Нужно разовое разрешение'],topic_override_available:true},
    {id:'card-1',name:'Карточка 1',desc:'',pos:10,language:'L1',media_type:'image',labels:[],product:null,format_labels:[],launch_status:'needs_full_preflight',reason_codes:[],reasons:[]},
    {id:'card-5',name:'Карточка 5',desc:'',pos:50,language:'L1',media_type:'image',labels:[],product:null,format_labels:[],launch_status:'needs_full_preflight',reason_codes:[],reasons:[]},
    {id:'card-2',name:'Карточка 2',desc:'',pos:20,language:'L1',media_type:'image',labels:[],product:null,format_labels:[],launch_status:'needs_full_preflight',reason_codes:[],reasons:[]},
    {id:'card-4',name:'Карточка 4',desc:'',pos:40,language:'L1',media_type:'image',labels:[],product:null,format_labels:[],launch_status:'needs_full_preflight',reason_codes:[],reasons:[]}
  ]
};
const __realFetch = window.fetch.bind(window);
window.fetch = async function(input, options = {}) {
  const url = new URL(String(input), location.href);
  if (url.origin !== location.origin) throw new Error('external network blocked: ' + url.origin);
  if (url.pathname === '/api/cards') {
    return new Response(JSON.stringify(window.__cardsPayload), {status:200,headers:{'Content-Type':'application/json'}});
  }
  if (url.pathname.startsWith('/api/launch/')) {
    const cardId = decodeURIComponent(url.pathname.split('/').pop());
    window.__lastLaunchCard = cardId;
    window.__launchRequests.push({cardId, method: options.method || 'GET', query: Object.fromEntries(url.searchParams)});
    document.getElementById('browser-request-proof').textContent = JSON.stringify(window.__launchRequests);
    if (cardId === 'card-4') {
      return new Response(JSON.stringify({detail:{status:'blocked',check_id:'check-card-4',reason_codes:['CAPACITY_BLOCKED'],reasons:['Нет безопасной ёмкости']}}), {status:409,headers:{'Content-Type':'application/json'}});
    }
    return new Response(JSON.stringify({status:'started',check_id:'check-'+cardId}), {status:200,headers:{'Content-Type':'application/json'}});
  }
  if (url.pathname === '/api/launch-stream') {
    const outcomes = { 'card-1':'succeeded', 'card-2':'partial', 'card-3':'blocked', 'card-5':'failed' };
    const outcome = outcomes[window.__lastLaunchCard] || 'failed';
    const payload = `event: status\ndata: ${JSON.stringify({running:false,outcome,progress:1,total:1,step:'',step_pct:null})}\n\nevent: done\ndata: ${JSON.stringify({outcome,check_id:'check-'+window.__lastLaunchCard,reason_codes:outcome==='blocked'?['TOPIC_VETO']:[],reasons:outcome==='blocked'?['Проверка заблокировала запуск']:[]})}\n\n`;
    return new Response(payload, {status:200,headers:{'Content-Type':'text/event-stream'}});
  }
  return new Response(JSON.stringify({}), {status:200,headers:{'Content-Type':'application/json'}});
};
</script>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib HTTP contract
        if self.path not in {"/", "/index.html"}:
            self.send_error(404)
            return
        html = INDEX.read_text(encoding="utf-8")
        # Browser fixture never asks the public internet for fonts/icons/charts.
        html = "\n".join(
            line
            for line in html.splitlines()
            if "fonts.googleapis.com" not in line
            and "fonts.gstatic.com" not in line
            and "cdnjs.cloudflare.com" not in line
            and "cdn.jsdelivr.net" not in line
        )
        html = html.replace("<head>", "<head>" + MOCK_SCRIPT, 1)
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
