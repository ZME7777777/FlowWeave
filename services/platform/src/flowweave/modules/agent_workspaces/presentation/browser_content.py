"""Rewrite preview resource references without granting platform origin access."""

from __future__ import annotations

import json
import re
from html import escape, unescape
from urllib.parse import urljoin, urlsplit

_JS_TOKEN = re.compile(
    r"//[^\n]*|/\*[\s\S]*?\*/|"
    r"(?P<string>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')|"
    r"`(?:\\.|[^`\\])*`|(?P<word>[A-Za-z_$][\w$]*)|(?P<punct>[^\s])"
)
_ATTRIBUTE = re.compile(
    r"(?P<name>\b(?:src|href|action|poster|data|srcset|style))\s*=\s*"
    r"(?P<quote>[\"'])(?P<value>.*?)(?P=quote)",
    re.IGNORECASE | re.DOTALL,
)
_HTML_PART = re.compile(
    r"<!--[\s\S]*?-->|<script\b[^>]*>[\s\S]*?</script\s*>|"
    r"<style\b[^>]*>[\s\S]*?</style\s*>|<[^>]+>",
    re.IGNORECASE,
)


class BrowserContent:
    def __init__(self, prefix: str, scheme: str, port: int, path: str):
        self.prefix = prefix
        self.origin = f"{scheme}://localhost:{port}"
        self.path = path

    def url(self, value: str) -> str:
        if not value or value.startswith(("#", self.prefix)):
            return value
        parsed = urlsplit(value)
        if parsed.scheme and parsed.scheme not in {"http", "https"}:
            return value
        if parsed.netloc:
            if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
                return value
            try:
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
            except ValueError:
                return value
            if port != urlsplit(self.origin).port:
                return value
        resolved = urlsplit(urljoin(self.origin + self.path, value))
        suffix = resolved.path.lstrip("/")
        if resolved.query:
            suffix += "?" + resolved.query
        if resolved.fragment:
            suffix += "#" + resolved.fragment
        return self.prefix + suffix

    def javascript(self, source: str) -> str:
        tokens = [token for token in _JS_TOKEN.finditer(source) if token.lastgroup is not None]
        replacements: list[tuple[int, int, str]] = []
        for index, token in enumerate(tokens):
            if token.lastgroup != "string":
                continue
            before = [part.group() for part in tokens[max(0, index - 2) : index]]
            after = [part.group() for part in tokens[index + 1 : index + 8]]
            is_import = bool(before and before[-1] in {"from", "import"}) or before == [
                "import",
                "(",
            ]
            is_asset = before == ["URL", "("] and after[:6] == [
                ",",
                "import",
                ".",
                "meta",
                ".",
                "url",
            ]
            is_asset_export = before == ["export", "default"]
            is_vite_css = before == ["__vite__css", "="]
            if is_vite_css:
                try:
                    css = json.loads(token.group())
                except ValueError:
                    continue
                replacements.append((token.start(), token.end(), json.dumps(self.css(css))))
                continue
            if not is_import and not is_asset and not is_asset_export:
                continue
            value = token.group()[1:-1]
            # Bare package imports are resolved by Vite or an import map.
            if not value.startswith(("/", "./", "../", "http://", "https://")):
                continue
            if "\\" not in value:
                replacements.append((token.start(), token.end(), json.dumps(self.url(value))))
        for start, end, value in reversed(replacements):
            source = source[:start] + value + source[end:]
        return source

    def css(self, source: str) -> str:
        def rewrite(match: re.Match[str]) -> str:
            value = match["value"].strip().strip("\"'")
            return match["start"] + json.dumps(self.url(value)) + match["end"]

        source = re.sub(
            r"(?P<start>url\(\s*)(?P<value>\"[^\"]*\"|'[^']*'|[^)]*)(?P<end>\))",
            rewrite,
            source,
            flags=re.IGNORECASE,
        )
        return re.sub(
            r"(?P<start>@import\s+)(?P<value>\"[^\"]*\"|'[^']*')(?P<end>)",
            rewrite,
            source,
            flags=re.IGNORECASE,
        )

    def html(self, source: str) -> str:
        def attribute(match: re.Match[str]) -> str:
            name = match["name"].lower()
            value = unescape(match["value"])
            if name == "style":
                value = self.css(value)
            elif name == "srcset" and not value.lstrip().startswith("data:"):
                value = ", ".join(
                    " ".join([self.url(parts[0]), *parts[1:]])
                    for entry in value.split(",")
                    if (parts := entry.split())
                )
            else:
                value = self.url(value)
            return f'{match["name"]}="{escape(value, quote=True)}"'

        def part(match: re.Match[str]) -> str:
            text = match.group()
            if text.startswith("<!--"):
                return text
            if re.match(r"<base\b", text, re.IGNORECASE):
                return ""
            end = text.index(">") + 1
            tag = _ATTRIBUTE.sub(attribute, text[:end])
            if re.match(r"<script\b", text, re.IGNORECASE):
                body, close = text[end:].rsplit("<", 1)
                return tag + self.javascript(body) + "<" + close
            if re.match(r"<style\b", text, re.IGNORECASE):
                body, close = text[end:].rsplit("<", 1)
                return tag + self.css(body) + "<" + close
            return tag

        source = _HTML_PART.sub(part, source)
        # Insert after rewriting so the generated base cannot be rewritten twice.
        base = f'<base href="{escape(self.url(self.path), quote=True)}">'
        bootstrap = '<meta name="referrer" content="no-referrer">' + self.bootstrap()
        source, count = re.subn(
            r"(<head\b[^>]*>)",
            lambda match: match[1] + base + bootstrap,
            source,
            count=1,
            flags=re.IGNORECASE,
        )
        return source if count else base + bootstrap + source

    def bootstrap(self) -> str:
        # Keep fetch/XHR in this Runtime's URL space, including Vite CSS updates.
        # The iframe stays opaque-origin: it never receives the login cookie.
        config = json.dumps({"prefix": self.prefix, "origin": self.origin, "path": self.path})
        return """<script>(()=>{
const c=CONFIG;
// Opaque previews have no persistent browser storage. Give applications an
// isolated, document-lifetime fallback rather than access to the host's data.
for(const name of ['localStorage','sessionStorage']){
  try{void window[name].length;}catch{
    const values=new Map();
    const api={getItem:key=>values.get(String(key))??null,
      setItem:(key,value)=>{values.set(String(key),String(value));},
      removeItem:key=>{values.delete(String(key));},clear:()=>values.clear(),
      key:index=>[...values.keys()][index]??null,get length(){return values.size;}};
    const storage=new Proxy(api,{
      get:(target,key)=>key in target?target[key]:values.get(String(key)),
      set:(_target,key,value)=>{values.set(String(key),String(value));return true;},
      ownKeys:()=>[...values.keys()],
      getOwnPropertyDescriptor:()=>({enumerable:true,configurable:true})
    });
    Object.defineProperty(window,name,{value:storage});
  }
}
const resolve=value=>{
  const raw=String(value);
  if(raw.startsWith(c.prefix))return raw;
  const u=new URL(raw,c.origin+c.path);
  if(!['localhost','127.0.0.1','[::1]'].includes(u.hostname)
      ||u.port!==new URL(c.origin).port)return raw;
  return c.prefix+u.pathname.slice(1)+u.search+u.hash;
};
const originalFetch=window.fetch;
window.fetch=(input,init)=>{
  const raw=input instanceof Request?input.url:String(input);
  const target=resolve(raw);
  const proxied=target.startsWith(c.prefix);
  return originalFetch(input instanceof Request?new Request(target,input):target,
    proxied?{...init,credentials:'omit'}:init);
};
const originalOpen=XMLHttpRequest.prototype.open;
const proxiedRequests=new WeakSet();
XMLHttpRequest.prototype.open=function(method,url,...rest){
  const target=resolve(url);
  if(target.startsWith(c.prefix))proxiedRequests.add(this);else proxiedRequests.delete(this);
  return originalOpen.call(this,method,target,...rest);
};
const originalSend=XMLHttpRequest.prototype.send;
XMLHttpRequest.prototype.send=function(...args){
  if(proxiedRequests.has(this))this.withCredentials=false;
  return originalSend.apply(this,args);
};
})();</script>""".replace("CONFIG", config.replace("<", "\\u003c"))

    def rewrite(self, content: bytes, content_type: str) -> bytes:
        mime = content_type.lower().split(";", 1)[0].strip()
        rewrite = (
            self.html
            if mime in {"text/html", "application/xhtml+xml"}
            else self.javascript
            if mime in {"text/javascript", "application/javascript", "application/x-javascript"}
            else self.css
            if mime == "text/css"
            else None
        )
        return rewrite(content.decode("utf-8", errors="replace")).encode() if rewrite else content
