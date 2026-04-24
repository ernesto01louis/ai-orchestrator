#!/usr/bin/env python3
import shutil, sys
from pathlib import Path

HTML = Path("/opt/ai-orchestrator/ui/graph.html")
BACKUP = HTML.with_suffix(".html.bak.step8")
MARKER = "/* KAZUKI-SPRITE-STEP8 */"

CSS_BLOCK = """  /* KAZUKI-SPRITE-STEP8 */
  .kazuki-sprite {
    width: 32px; height: 44px; flex-shrink: 0;
    background-image: url("assets/kazuki/kazuki_sprites.png");
    background-size: 224px 44px;
    background-position: 0 0;
    background-repeat: no-repeat;
    border-radius: 4px;
    border: 1px solid var(--accent-dim);
    transition: background-position 250ms ease-out;
    image-rendering: auto;
  }
  .kazuki-sprite[data-state="thinking"]  { background-position: -32px 0; }
  .kazuki-sprite[data-state="alert"]     { background-position: -64px 0; }
  .kazuki-sprite[data-state="speaking"]  { background-position: -96px 0; }
  .kazuki-sprite[data-state="success"]   { background-position: -128px 0; }
  .kazuki-sprite[data-state="error"]     { background-position: -160px 0; }
  .kazuki-sprite[data-state="dream"]     { background-position: -192px 0; }
  /* /KAZUKI-SPRITE-STEP8 */"""

AVATAR_HTML = (
    '<div class="kazuki-sprite" id="kazuki-avatar" data-state="idle"></div>'
    '<img src="assets/kazuki/kazuki_sprites.png" style="display:none" '
    """onerror="var a=document.getElementById('kazuki-avatar');"""
    """if(a)a.outerHTML='<div class=\\'avatar-placeholder\\'>KZ</div>'">\n"""
)

JS_BLOCK = """
        // KAZUKI-SPRITE-STEP8: avatar state from ws status
        var kza = document.getElementById('kazuki-avatar');
        if (kza) {
          var ks = 'idle';
          if (msg.completed) {
            ks = msg.has_error ? 'error' : 'success';
          } else if (isRunning) {
            var ph = (msg.phase || '').toLowerCase();
            if (ph.indexOf('hindsight') >= 0 || ph.indexOf('live context') >= 0 || ph.indexOf('mental model') >= 0 || ph.indexOf('profil') >= 0) ks = 'thinking';
            else if (ph.indexOf('llm') >= 0 || ph.indexOf('generat') >= 0 || ph.indexOf('writing') >= 0) ks = 'speaking';
            else if (ph.indexOf('deploy') >= 0 || ph.indexOf('verif') >= 0 || ph.indexOf('scan') >= 0 || ph.indexOf('tool') >= 0) ks = 'alert';
            else ks = 'speaking';
          } else {
            ks = 'dream';
          }
          kza.dataset.state = ks;
        }
        // /KAZUKI-SPRITE-STEP8"""

def main():
    src = HTML.read_text()
    if MARKER in src:
        print("\u00b7 kazuki sprite patch already applied")
        return 0

    shutil.copy2(HTML, BACKUP)
    print("\u00b7 backup at " + str(BACKUP))

    anchor_css = "#status-dot {"
    if anchor_css not in src:
        print("ERR: CSS anchor not found")
        return 1
    src = src.replace(anchor_css, CSS_BLOCK + "\n  " + anchor_css)
    print("\u2713 CSS injected")

    old_html = '<div class="avatar-placeholder">KZ</div>'
    if old_html not in src:
        print("ERR: avatar placeholder not found")
        return 1
    src = src.replace(old_html, AVATAR_HTML)
    print("\u2713 HTML replaced")

    anchor_js = "activeNodeIds = nodeIds;\n        updateActiveEdges();"
    if anchor_js not in src:
        print("ERR: JS anchor not found")
        return 1
    src = src.replace(anchor_js, anchor_js + JS_BLOCK, 1)
    print("\u2713 JS injected")

    HTML.write_text(src)
    print("\u2713 graph.html patched")
    return 0

if __name__ == "__main__":
    sys.exit(main())
