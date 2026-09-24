# -*- coding: utf-8 -*-
"""pyflow：静态分析 Python 脚本，生成标准流程图网页（单文件工具）。

用法：
    python flowgen.py <目标.py> [输出.html]

生成一个自包含 HTML（数据 + 布局引擎全部内嵌，零外部依赖），浏览器打开后：
  - 初始只显示「模块入口 + 未被调用的函数」的流程图；
  - 点击语句旁的金色调用标记，在调用点原位置展开被调函数的流程图；
  - 展开图内部同样可点，递归展开，再点一次收起。

设计要点：
  - 代码与注释按源文件原文显示（去前导缩进，不增删换行）；
  - 主流程竖向延伸，分支横向延伸，正交折线连线，通道独立不重叠。
"""
import ast
import io
import json
import sys
import tokenize

# ---------- 配置区 ----------
DEFAULT_OUTPUT = "flow.html"
TERMINAL_KINDS = {"return", "raise", "break", "continue"}


# ---------- 基础设施区 ----------
def log(msg, level="info"):
    print(f"[{level}] {msg}")


def read_source(path):
    """读源码：utf-8 优先，失败回退 gbk。"""
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"无法解码文件: {path}")


def collect_comments(src):
    """tokenize 收集全部注释：[(行号, 文本)]。"""
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            out.append((tok.start[0], tok.string.strip()))
    return out


def covered_lines(tree):
    """所有语句/函数头覆盖的行号集合（含装饰器行）。"""
    cov = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            cov.add(node.lineno)
            for dec in node.decorator_list:
                cov.add(dec.lineno)
        elif isinstance(node, ast.stmt) and getattr(node, "end_lineno", None):
            cov.update(range(node.lineno, node.end_lineno + 1))
    return cov


def assign_comments(src, tree):
    """把不属于任何语句行范围的注释，就近分配给下一个起始点。

    返回 (pre_map, head_map, tail)：
      pre_map  行号 -> 注释列表（挂到该行起始的语句上方）
      head_map 行号 -> 注释列表（挂到该行起始的函数头）
      tail     无归属注释 [(行号, 文本)]（挂到所在函数末尾说明）
    """
    cov = covered_lines(tree)
    free = [(r, t) for r, t in collect_comments(src) if r not in cov]
    anchors = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            anchors.setdefault(node.lineno, "def")
        elif isinstance(node, ast.stmt):
            anchors.setdefault(node.lineno, "stmt")
    starts = sorted(anchors)
    pre_map, head_map, tail = {}, {}, []
    for row, text in free:
        nxt = next((s for s in starts if s > row), None)
        if nxt is None:
            tail.append((row, text))
        elif anchors[nxt] == "def":
            head_map.setdefault(nxt, []).append(text)
        else:
            pre_map.setdefault(nxt, []).append(text)
    return pre_map, head_map, tail


# ---------- 业务区：语句树 ----------
def collect_calls(node, fnames):
    """收集节点内的本地函数调用（跳过嵌套 def / lambda / class 作用域）。"""
    out = []

    def rec(n):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in fnames:
            if n.func.id not in out:
                out.append(n.func.id)
        for child in ast.iter_child_nodes(n):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            rec(child)

    rec(node)
    return out


def src_lines_of(node, src_lines):
    """语句源码行原文（去前导/尾部空白，保留行尾注释与原有换行）。"""
    end = getattr(node, "end_lineno", None) or node.lineno
    return [src_lines[i].strip() for i in range(node.lineno - 1, end)]


def ends_terminal(body_nodes):
    return bool(body_nodes) and body_nodes[-1].get("k") in TERMINAL_KINDS


class Converter:
    """语句树 -> JSON 节点。节点字段：
    叶子: {k, lines, cm, c?}        k: stmt/return/break/continue/raise/assert
    分支: {k:"if", br:[{l, b, t, e?}]}
    循环: {k:"while"|"for", h, b, e}   异常: {k:"try", b, hd, e, f}
    上下文: {k:"with", h, b}        嵌套定义: {k:"def", lines, cm}
    """

    def __init__(self, src_lines, pre_map, short2q):
        self.src = src_lines
        self.pre = pre_map
        self.s2q = short2q
        self.fnames = set(short2q)

    def body(self, stmts):
        out = []
        for st in stmts:
            node = self.stmt(st)
            if node is not None:
                out.append(node)
        return out

    def stmt(self, st):
        cm = self.pre.pop(st.lineno, [])
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
            lines = [self.src[d.lineno - 1].strip() for d in st.decorator_list]
            lines.append(self.src[st.lineno - 1].strip())
            return {"k": "def", "lines": lines, "cm": cm}
        if isinstance(st, ast.If):
            brs = []
            cur = st
            while True:
                body = self.body(cur.body)
                brs.append({"l": self.src[cur.lineno - 1].strip(), "b": body,
                            "t": ends_terminal(body)})
                orelse = cur.orelse
                if len(orelse) == 1 and isinstance(orelse[0], ast.If):
                    cur = orelse[0]
                    continue
                if orelse:
                    body2 = self.body(orelse)
                    brs.append({"l": "else:", "b": body2, "t": ends_terminal(body2), "e": 1})
                break
            return {"k": "if", "br": brs, "cm": cm}
        if isinstance(st, (ast.While, ast.For)):
            orelse = self.body(st.orelse)
            return {"k": type(st).__name__.lower(), "h": self.src[st.lineno - 1].strip(),
                    "b": self.body(st.body), "e": orelse, "cm": cm}
        if isinstance(st, ast.Try) or (hasattr(ast, "TryStar") and isinstance(st, ast.TryStar)):
            hd = []
            for h in st.handlers:
                hb = self.body(h.body)
                hd.append({"l": self.src[h.lineno - 1].strip(), "b": hb, "t": ends_terminal(hb)})
            return {"k": "try", "b": self.body(st.body), "hd": hd,
                    "e": self.body(st.orelse), "f": self.body(st.finalbody), "cm": cm}
        if isinstance(st, (ast.With, ast.AsyncWith)):
            return {"k": "with", "h": self.src[st.lineno - 1].strip(),
                    "b": self.body(st.body), "cm": cm}
        if isinstance(st, ast.Return):
            k = "return"
        elif isinstance(st, ast.Break):
            k = "break"
        elif isinstance(st, ast.Continue):
            k = "continue"
        elif isinstance(st, ast.Raise):
            k = "raise"
        elif isinstance(st, ast.Assert):
            k = "assert"
        else:
            k = "stmt"
        node = {"k": k, "lines": src_lines_of(st, self.src), "cm": cm}
        # 调用统一存限定名（嵌套函数短名可能与顶层冲突，前端按 qname 定位展开目标）
        calls = [self.s2q[name] for name in collect_calls(st, self.fnames)]
        if calls:
            node["c"] = calls
        return node


# ---------- 业务区：函数提取与调用图 ----------
def register_functions(tree, src_lines):
    """两遍法：先注册全部函数名（短名 -> 限定名），返回 (short2q, defs)。"""
    short2q, defs = {}, []

    def reg(fd, prefix):
        q = f"{prefix}.{fd.name}" if prefix else fd.name
        short2q.setdefault(fd.name, q)
        defs.append((fd, q, prefix))
        for st in fd.body:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                reg(st, q)

    for st in tree.body:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
            reg(st, "")
    return short2q, defs


def tree_calls(nodes, acc):
    for n in nodes:
        for c in n.get("c", []) or []:
            acc.append(c)
        for key in ("b", "e", "f"):
            if n.get(key):
                tree_calls(n[key], acc)
        if n.get("br"):
            for br in n["br"]:
                tree_calls(br["b"], acc)
        if n.get("hd"):
            for hd in n["hd"]:
                tree_calls(hd["b"], acc)


def build_data(path):
    src = read_source(path)
    tree = ast.parse(src)
    src_lines = src.splitlines()
    pre_map, head_map, tail = assign_comments(src, tree)
    short2q, defs = register_functions(tree, src_lines)
    conv = Converter(src_lines, pre_map, short2q)

    funcs = []
    for fd, q, _prefix in defs:
        head_lines = [src_lines[d.lineno - 1].strip() for d in fd.decorator_list]
        head_lines.append(src_lines[fd.lineno - 1].strip())
        f = {"q": q, "name": fd.name, "head": head_lines,
             "cm": head_map.pop(fd.lineno, []), "line": fd.lineno,
             "b": conv.body(fd.body), "calls": [], "by": []}
        tree_calls(f["b"], f["calls"])
        f["calls"] = list(dict.fromkeys(f["calls"]))
        funcs.append(f)

    # 模块级语句（跳过函数定义，它们已作为独立函数图展示）
    module_body = conv.body([st for st in tree.body
                             if not isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef))])
    module_calls = []
    tree_calls(module_body, module_calls)

    # 尾注释就近挂到包含它的函数
    for row, text in tail:
        cand = [f for f in funcs if f["line"] < row]
        if cand:
            max(cand, key=lambda f: f["line"]).setdefault("tail", []).append(text)

    # 入口判定：未被任何「函数」调用的 def（模块级调用不影响，main 始终算入口）
    # 类方法不作为独立函数图分析，但其体内的本地函数调用计入 called，避免误判入口
    called = set()
    for f in funcs:
        called.update(f["calls"])
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for st in node.body:
                if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    called.update(short2q[c] for c in collect_calls(st, set(short2q)) if c in short2q)
    by_map = {}
    for f in funcs:
        for target in f["calls"]:
            by_map.setdefault(target, set()).add(f["name"])
    for f in funcs:
        f["by"] = sorted(by_map.get(f["q"], set()))
        f["entry"] = f["q"] not in called

    module = {"q": "__module__", "name": "__module__",
              "head": ["# 模块入口（模块级代码）"], "cm": [], "b": module_body,
              "calls": list(dict.fromkeys(module_calls)), "by": [], "entry": True}

    entries = []
    if module_body:
        entries.append("__module__")
    entries += [f["q"] for f in funcs if f["entry"]]
    all_funcs = ([module] if module_body else []) + funcs
    return {"file": path.replace("\\", "/").rsplit("/", 1)[-1], "functions": all_funcs,
            "entries": entries}


# ---------- HTML 模板 ----------
TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>pyflow - __FILE__</title>
<style>
:root{
  --bg:#14161a; --panel:#1b1e24; --line:#2a2e37; --txt:#c9d1d9; --dim:#8b95a5;
  --gold:#c9a227; --gold-dim:#8a7326;
}
*{box-sizing:border-box; margin:0; padding:0}
html,body{height:100%; overflow:hidden}
body{background:var(--bg); color:var(--txt); font-family:"Segoe UI",system-ui,sans-serif; display:flex; flex-direction:column}
header{display:flex; align-items:center; gap:14px; padding:8px 16px; background:var(--panel); border-bottom:1px solid var(--line); flex:0 0 auto; flex-wrap:wrap}
header h1{font-size:14px; font-weight:600; color:var(--gold)}
header .file{font-family:Consolas,monospace; font-size:12px; color:var(--dim)}
.zoom{display:flex; gap:4px; margin-left:auto}
.zoom button{background:#232830; color:var(--txt); border:1px solid var(--line); border-radius:5px; padding:3px 10px; cursor:pointer; font-size:12px}
.zoom button:hover{border-color:var(--gold); color:var(--gold)}
.idx{padding:8px 16px; background:#171a1f; border-bottom:1px solid var(--line); flex:0 0 auto}
#side{width:200px; flex:0 0 auto; background:var(--panel); border-right:1px solid var(--line); overflow-y:auto; padding:12px 8px; scrollbar-width:none; -ms-overflow-style:none}
#side::-webkit-scrollbar{display:none; width:0; height:0}
#side .lb{font-size:11px; color:var(--dim); padding:0 8px 8px; letter-spacing:1px}
#side .fn{display:block; width:100%; text-align:left; padding:5px 10px; margin:1px 0; border-radius:6px; cursor:pointer; font-family:Consolas,"Courier New",monospace; font-size:12px; color:var(--dim); border:1px solid transparent; white-space:nowrap; overflow:hidden; text-overflow:ellipsis}
#side .fn .star{color:var(--gold); margin-left:5px}
#side .fn.entry{color:#e2c96a; border-color:rgba(201,162,39,.30)}
#side .fn:hover{background:#232830}
#side .fn.active{background:#2a2416; border-color:var(--gold); color:var(--gold)}
#main{flex:1 1 auto; display:flex; min-height:0}
#wrap{flex:1 1 auto; min-width:0; overflow:hidden; position:relative; cursor:grab; user-select:none; -webkit-user-select:none; background:
  radial-gradient(circle at 50% 30%, #191c22 0%, var(--bg) 70%)}
#wrap.dragging{cursor:grabbing}
#cv{transform-origin:0 0; will-change:transform}
#cv svg{display:block}
.legend{position:fixed; right:14px; bottom:12px; background:rgba(23,26,31,.92); border:1px solid var(--line); border-radius:8px; padding:8px 12px; font-size:11px; color:var(--dim); line-height:1.9; z-index:5}
.legend i{display:inline-block; width:14px; height:9px; margin-right:6px; vertical-align:middle; border:1px solid}
.legend .l1{background:#232830; border-color:#454d5a}
.legend .l2{background:#262233; border-color:#8a6fc0; transform:rotate(0)}
.legend .l3{background:#2b2416; border-color:var(--gold); border-radius:5px}
.legend .l4{background:#33262a; border-color:#c1666b}
svg text{font-family:Consolas,"Courier New",monospace; font-size:13px}
text.nt{fill:#d6dbe3}
text.cmt{fill:#7e8f5a; font-style:italic}
text.dm{fill:#d9cdf2}
text.pl{fill:#e8d9a8}
text.bx{fill:var(--gold); font-size:12px}
text.lb{fill:#9aa0aa; font-size:11px}
text.lp{fill:#6fa8dc; font-size:11px}
text.tl{fill:var(--dim); font-size:12px; font-weight:bold}
.nd-stmt rect.bx2{fill:#232830; stroke:#454d5a}
.nd-return rect.bx2{fill:#33262a; stroke:#c1666b}
.nd-return text.nt{fill:#f0c9c9}
.nd-break rect.bx2,.nd-continue rect.bx2,.nd-raise rect.bx2{fill:#2e2a22; stroke:#b08d3e}
.nd-assert rect.bx2{fill:#232830; stroke:#454d5a}
.nd-def rect.bx2{fill:#242a2e; stroke:#4a8fa8}
.nd-def text.nt{fill:#a8d4e8}
.dia polygon{fill:#262233; stroke:#8a6fc0; stroke-width:1.2}
.pill rect{fill:#2b2416; stroke:var(--gold); stroke-width:1.2}
.boxx rect.frame{fill:rgba(201,162,39,.03); stroke:var(--gold); stroke-dasharray:6 4; rx:8}
.boxx rect.frame{rx:8}
.edge{stroke:#8b95a5; stroke-width:1.3; fill:none}
.edge.lp{stroke:#6fa8dc}
.edge.term{stroke:#c1666b}
g.chip{cursor:pointer}
g.chip rect{fill:#1f2733; stroke:var(--gold); rx:9}
g.chip text{fill:#e2c96a; font-size:11px}
g.chip:hover rect{fill:#2c3542; stroke:#e2c96a}
</style>
</head>
<body>
<header>
  <h1>pyflow 流程图</h1>
  <span class="file" id="finfo"></span>
  <div class="zoom">
    <button onclick="zoomBy(0.85)">-</button>
    <button onclick="zoomBy(1.18)">+</button>
    <button onclick="fitWidth()">适应宽度</button>
    <button onclick="resetView()">复位</button>
  </div>
</header>
<div id="main">
  <aside id="side"><div class="lb">函数索引</div><div id="idx"></div></aside>
  <div id="wrap"><div id="cv"></div></div>
</div>
<div class="legend">
  <div><i class="l3"></i>开始 / 结束（入口与函数）</div>
  <div><i class="l1"></i>处理语句 &nbsp;<i class="l2" style="width:16px;height:12px"></i>判断</div>
  <div><i class="l4"></i>返回 / 终止 &nbsp;金色虚线框 = 已展开函数</div>
</div>
<script id="FD" type="application/json">__PAYLOAD__</script>
<script>
"use strict";
const DATA = JSON.parse(document.getElementById("FD").textContent);
document.getElementById("finfo").textContent = DATA.file + " · " + DATA.functions.length + " 个函数 · 入口 " + DATA.entries.length + " 个";

/* ============ 布局常量 ============ */
const FS=13, LH=19, PADX=14, PADY=9, GAP=34, LINK_H=44, MERGE=25, ROW_GAP=26,
      LOOP_R=24, LOOP_L=52, TAIL=24, SEC=100, CHIP_H=20;
function tw(s){let w=0;for(const ch of String(s)){const c=ch.codePointAt(0);w+=(c>0x2E7F)?FS:FS*0.56;}return w;}
function maxW(lines){let m=0;for(const l of lines)m=Math.max(m,tw(l.t!=null?l.t:l));return m;}
function diaSize(lines){return {w:maxW(lines)+96+30, h:lines.length*LH+34+12};}
function esc(s){return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");}

/* 块结构：{L,R,h,nodes,edges,exit}  轴=0，entry=(0,0)，exit=(0,h) 或 null */
const ctx = {funcs:{}, exp:new Set(), sections:[]};
DATA.functions.forEach(f=>ctx.funcs[f.q]=f);

function tr(n,dx,dy){
  if(n.t==="diamond"){n.cx+=dx;n.cy+=dy;return n;}
  if(n.cx!=null){n.cx+=dx;n.cy+=dy;return n;}
  n.x+=dx; n.y+=dy; return n;
}
function trE(e,dx,dy){e.pts=e.pts.map(p=>[p[0]+dx,p[1]+dy]);
  if(e.lx!=null){e.lx+=dx;e.ly+=dy;} return e;}

function compose(parts){
  const nodes=[],edges=[]; let y=0,prevExit=null,L=8,R=8;
  parts.forEach((b,i)=>{
    b.nodes.forEach(n=>nodes.push(tr(n,0,y)));
    b.edges.forEach(e=>edges.push(trE(e,0,y)));
    if(i>0 && prevExit) edges.push({pts:[[0,prevExit],[0,y]],a:1});
    prevExit = b.exit? y+b.h : null;
    L=Math.max(L,b.L); R=Math.max(R,b.R);
    y+=b.h+GAP;
  });
  const h = parts.length? y-GAP : 0;
  const last = parts[parts.length-1];
  return {L,R,h,nodes,edges,exit:(last&&last.exit)?[0,h]:null};
}

/* ============ 各语句布局 ============ */
function leafBlock(lines,cls){
  const w=maxW(lines)+2*PADX, h=lines.length*LH+2*PADY;
  return {L:w/2+8,R:w/2+8,h,nodes:[{t:"rect",x:-w/2,y:0,w,h,lines,cls}],
          edges:[],exit:[0,h]};
}

function layoutLeaf(s,pfx){
  const lines = (s.cm||[]).map(t=>({t,c:"cmt"})).concat((s.lines||[]).map(t=>({t})));
  const w=maxW(lines)+2*PADX, h=lines.length*LH+2*PADY;
  const nodes=[{t:"rect",x:-w/2,y:0,w,h,lines,cls:"nd-"+s.k}];
  const edges=[]; let L=w/2+8, R=w/2+8;
  const term = (s.k==="return"||s.k==="raise"||s.k==="break"||s.k==="continue");
  const calls = s.c||[];
  let cx=w/2+10;
  calls.forEach(fn=>{
    const dn=fn.split(".").pop();   // 显示短名，data-fn 存限定名
    const cw=tw("⊙ "+dn)+20;
    nodes.push({t:"chip",x:cx,y:(h-CHIP_H)/2,w:cw,h:CHIP_H,fn,dn,path:pfx});
    cx+=cw+8;
  });
  R=Math.max(R,cx+2); L=Math.max(L,w/2+8);
  let hTot=h, boxTop=null;
  const boxes = calls.filter(fn=>ctx.exp.has(pfx+"::"+fn)).map(fn=>{
    const f=ctx.funcs[fn];
    const sub=layoutSeq(f.b, pfx+"::"+fn+"/s");
    const bw=sub.L+sub.R+30, bh=26+sub.h+18;
    return {fn,sub,bw,bh,L:sub.L,R:sub.R};
  });
  boxes.forEach((b,i)=>{
    const top = hTot+28;
    nodes.push({t:"box",x:-(b.L+15),y:top,w:b.bw,h:b.bh,title:b.fn});
    edges.push({pts:[[0,hTot],[0,top]],a:1});
    b.sub.nodes.forEach(n=>nodes.push(tr(n,0,top+26)));
    b.sub.edges.forEach(e=>edges.push(trE(e,0,top+26)));
    hTot = top+b.bh;
    L=Math.max(L,b.L+15); R=Math.max(R,b.R+15);
  });
  return {L,R,h:hTot,nodes,edges,exit:term?null:(boxes.length?[0,hTot]:[0,h])};
}

function layoutIf(s,pfx){
  const brs=s.br, hasElse = !!(brs[brs.length-1]||{}).e;
  const diaBr = hasElse? brs.slice(0,-1) : brs;
  const bodyB = brs.map((br,i)=>layoutSeq(br.b, pfx+"/b"+i));
  const diaB = diaBr.map(br=>diaSize([br.l]));
  const nodes=[],edges=[]; let rowTop=0, L=20, R=20, rows=[];
  for(let i=0;i<diaB.length;i++){
    const d=diaB[i], b=bodyB[i];
    const rowH=Math.max(d.h,b.h), dCy=rowTop+d.h/2;
    let bTop=dCy-b.h/2;               // 矮分支体垂直居中于菱形，箭头正对左缘中心
    if(bTop<rowTop) bTop=rowTop;      // 高分支体（含展开盒子）顶对齐，防止上溢
    rows.push({top:rowTop,d,dCy,b,bTop,rowH,P:rowTop+rowH+MERGE});
    rowTop += rowH+MERGE+ROW_GAP;
  }
  const lastP = diaB.length? rows[rows.length-1].P : 0;
  let elseTop = lastP+ROW_GAP, elseB=null;
  if(hasElse){ elseB=bodyB[bodyB.length-1]; }
  const elseBot = hasElse? elseTop+elseB.h : elseTop;
  const exitY = (hasElse? elseBot : lastP) + TAIL;
  rows.forEach((r,i)=>{
    nodes.push({t:"diamond",cx:0,cy:r.dCy,hw:r.d.w/2,hh:r.d.h/2,lines:[{t:diaBr[i].l}]});
    const pos = r.d.w/2 + LINK_H + r.b.L;   // 分支体左缘
    r.b.nodes.forEach(n=>nodes.push(tr(n,pos,r.bTop)));
    r.b.edges.forEach(e=>edges.push(trE(e,pos,r.bTop)));
    edges.push({pts:[[r.d.w/2,r.dCy],[pos,r.dCy]],a:1,lbl:"是",lx:r.d.w/2+8,ly:r.dCy-7});
    edges.push({pts:[[0,r.top+r.d.h],[0,r.P]]});
    if(r.b.exit) edges.push({pts:[[pos,r.bTop+r.b.h],[pos,r.P],[0,r.P]]});
    const nxt = i+1<rows.length? rows[i+1].top : (hasElse? elseTop : exitY);
    edges.push({pts:[[0,r.P],[0,nxt]],a:1,lbl:"否",lx:6,ly:r.top+r.d.h+13});
    L=Math.max(L,r.d.w/2+26);
    R=Math.max(R,pos+r.b.R+26);
  });
  if(hasElse){
    elseB.nodes.forEach(n=>nodes.push(tr(n,0,elseTop)));
    elseB.edges.forEach(e=>edges.push(trE(e,0,elseTop)));
    L=Math.max(L,elseB.L+14); R=Math.max(R,elseB.R+14);
    edges.push({pts:[[0,elseBot],[0,exitY]],a:1});
  }
  return {L,R,h:exitY+2,nodes,edges,exit:[0,exitY+2]};
}

function layoutLoop(s,pfx){
  const d=diaSize([s.h]);
  const body=layoutSeq(s.b, pfx+"/b");
  const bodyTop=d.h+30, bodyBot=bodyTop+body.h;
  const hasE=s.e&&s.e.length;
  const eB=hasE? layoutSeq(s.e,pfx+"/e") : null;
  const yF=bodyBot+LOOP_R+30, eTop=yF+26;
  const eBot=hasE? eTop+eB.h : eTop;
  const h=eBot+2;
  const xL=Math.max(d.w/2,body.L)+LOOP_L, xR=Math.max(d.w/2,body.R)+LOOP_L;
  const nodes=[{t:"diamond",cx:0,cy:d.h/2,hw:d.w/2,hh:d.h/2,lines:[{t:s.h}]}];
  const edges=[];
  body.nodes.forEach(n=>nodes.push(tr(n,0,bodyTop)));
  body.edges.forEach(e=>edges.push(trE(e,0,bodyTop)));
  edges.push({pts:[[0,d.h],[0,bodyTop]],a:1,lbl:"是",lx:5,ly:d.h+13});
  if(body.exit) edges.push({pts:[[0,bodyBot],[0,bodyBot+LOOP_R],[-xL,bodyBot+LOOP_R],
    [-xL,d.h/2],[-d.w/2,d.h/2]],a:1,cls:"lp",lbl:"循环",lx:-xL-8,ly:(d.h/2+bodyBot)/2,la:"end"});
  edges.push({pts:[[d.w/2,d.h/2],[xR,d.h/2],[xR,yF],[0,yF],[0,eTop]],a:1,
    lbl:"否",lx:d.w/2+8,ly:d.h/2-7});
  if(hasE){ eB.nodes.forEach(n=>nodes.push(tr(n,0,eTop)));
            eB.edges.forEach(e=>edges.push(trE(e,0,eTop))); }
  return {L:xL+10,R:xR+10,h,nodes,edges,exit:[0,h]};
}

function layoutTry(s,pfx){
  const tb=layoutSeq(s.b,pfx+"/b");
  const d=diaSize(["发生异常?"]);
  const dTop=tb.h+30, dCy=dTop+d.h/2, dBot=dTop+d.h;
  const hs=s.hd.map((hd,i)=>layoutSeq([{k:"stmt",lines:[hd.l],cm:[]}].concat(hd.b), pfx+"/h"+i));
  const nodes=[],edges=[];
  tb.nodes.forEach(n=>nodes.push(tr(n,0,0)));
  tb.edges.forEach(e=>edges.push(trE(e,0,0)));
  nodes.push({t:"diamond",cx:0,cy:dCy,hw:d.w/2,hh:d.h/2,lines:[{t:"发生异常?"}]});
  if(tb.exit) edges.push({pts:[[0,tb.h],[0,dTop]],a:1});
  // handler 横排
  let cum=d.w/2+LINK_H; const cxs=[];
  hs.forEach(hb=>{ cxs.push(cum+hb.L); cum+=hb.L+hb.R+30; });
  const hTop=dCy+16;
  const hBottoms=[], hRights=[];
  hs.forEach((hb,k)=>{
    hb.nodes.forEach(n=>nodes.push(tr(n,cxs[k],hTop)));
    hb.edges.forEach(e=>edges.push(trE(e,cxs[k],hTop)));
    edges.push({pts:[[cxs[k],dCy],[cxs[k],hTop]],a:1});
    hBottoms.push(hTop+hb.h); hRights.push(cxs[k]+hb.R);
  });
  const hasE=s.e&&s.e.length;
  const eB=hasE? layoutSeq(s.e,pfx+"/e") : null;
  const eTop=dBot+26, eBot=hasE? eTop+eB.h : dBot;
  const hasF=s.f&&s.f.length;
  const fB=hasF? layoutSeq(s.f,pfx+"/f") : null;
  const yH=hBottoms.length? Math.max(...hBottoms)+20 : 0;
  const mergeY=Math.max(dBot,eBot,yH)+26;
  if(hs.length){
    const minCx=cxs[0], maxCx=cxs[cxs.length-1];
    edges.push({pts:[[d.w/2,dCy],[maxCx,dCy]],lbl:"是",lx:d.w/2+8,ly:dCy-7});
    hs.forEach((hb,k)=>{ if(hb.exit) edges.push({pts:[[cxs[k],hTop+hb.h],[cxs[k],yH]]}); });
    const orelseHits = hasE && yH>eTop && yH<eBot;   // 汇流横线会穿过 try-else 体时才右侧绕行
    if(orelseHits){
      edges.push({pts:[[minCx,yH],[maxCx,yH]]});
      const xTry=Math.max(...hRights,d.w/2)+30;
      edges.push({pts:[[maxCx,yH],[xTry,yH],[xTry,mergeY],[0,mergeY]]});
    }else{
      edges.push({pts:[[minCx,yH],[0,yH]]});          // 直接横向汇回中轴
      edges.push({pts:[[0,yH],[0,mergeY]]});
    }
  }
  edges.push({pts:[[0,dBot],[0,hasE?eTop:mergeY]],a:hasE?1:0,lbl:"否",lx:5,ly:dBot+13});  // 无 else 时直达汇合点，不断线
  if(hasE){ eB.nodes.forEach(n=>nodes.push(tr(n,0,eTop)));
            eB.edges.forEach(e=>edges.push(trE(e,0,eTop)));
            edges.push({pts:[[0,eBot],[0,mergeY]]}); }
  if(hasF){
    const fTop=mergeY+26;
    fB.nodes.forEach(n=>nodes.push(tr(n,0,fTop)));
    fB.edges.forEach(e=>edges.push(trE(e,0,fTop)));
    edges.push({pts:[[0,mergeY],[0,fTop]],a:1});
    edges.push({pts:[[0,fTop+fB.h],[0,fTop+fB.h+TAIL]],a:1});
    var h=fTop+fB.h+TAIL+2;
  }else{
    edges.push({pts:[[0,mergeY],[0,mergeY+TAIL]],a:1});
    var h=mergeY+TAIL+2;
  }
  const L=Math.max(tb.L,d.w/2,(hasF?fB.L:0))+20;
  const R=Math.max(tb.R,d.w/2,hRights.length?Math.max(...hRights)+50:0,(hasF?fB.R:0))+20;
  return {L,R,h,nodes,edges,exit:[0,h]};
}

function layoutSeq(stmts,pfx){
  return compose(stmts.map((s,i)=>layoutStmt(s,pfx+"/"+i)));
}
function layoutStmt(s,pfx){
  switch(s.k){
    case "if": return layoutIf(s,pfx);
    case "while": case "for": return layoutLoop(s,pfx);
    case "try": return layoutTry(s,pfx);
    case "with": return compose([leafBlock([{t:s.h}],"nd-stmt"), layoutSeq(s.b,pfx+"/b")]);
    case "def": {const b=leafBlock((s.cm||[]).map(t=>({t,c:"cmt"})).concat(s.lines.map(t=>({t}))),"nd-def");
                 return b;}
    default: return layoutLeaf(s,pfx);
  }
}

/* ============ SVG 渲染 ============ */
function textBlock(x,y,lines,cls,anchor){
  const a=anchor||"middle";
  let t=`<text x="${x}" y="${y}" text-anchor="${a}" class="${cls}">`;
  lines.forEach((ln,i)=>{
    t+=`<tspan x="${x}" dy="${i?LH:0}"${ln.c?` class="${ln.c}"`:""}>${esc(ln.t)}</tspan>`;
  });
  return t+"</text>";
}
function nodeSvg(n){
  switch(n.t){
    case "rect":{
      const baseY=n.y+PADY+LH*0.78;
      return `<g class="${n.cls}"><rect class="bx2" x="${n.x}" y="${n.y}" width="${n.w}" height="${n.h}" rx="6"/>`
        + textBlock(n.x+PADX, baseY, n.lines, "nt", "start") + `</g>`;
    }
    case "pill":{
      const baseY=n.y+PADY+LH*0.78;
      return `<g class="pill"><rect x="${n.x}" y="${n.y}" width="${n.w}" height="${n.h}" rx="${n.h/2}"/>`
        + textBlock(n.x+n.h/2+8, baseY, n.lines, "pl", "start") + `</g>`;
    }
    case "diamond":{
      const p=`${n.cx-n.hw},${n.cy} ${n.cx},${n.cy-n.hh} ${n.cx+n.hw},${n.cy} ${n.cx},${n.cy+n.hh}`;
      const mw=maxW(n.lines);
      return `<g class="dia"><polygon points="${p}"/>`
        + textBlock(n.cx-mw/2, n.cy-n.lines.length*LH/2+LH*0.78, n.lines, "dm", "start") + `</g>`;
    }
    case "box":{
      return `<g class="boxx"><rect class="frame" x="${n.x}" y="${n.y}" width="${n.w}" height="${n.h}" rx="8"/>`
        + `<text class="bx" x="${n.x+12}" y="${n.y+17}">▸ 展开函数：${esc(n.title)}()</text></g>`;
    }
    case "chip":{
      return `<g class="chip" data-path="${esc(n.path)}" data-fn="${esc(n.fn)}">`
        + `<rect x="${n.x}" y="${n.y}" width="${n.w}" height="${n.h}" rx="9"/>`
        + `<text x="${n.x+n.w/2}" y="${n.y+n.h/2+4}" text-anchor="middle">⊙ ${esc(n.dn)}</text></g>`;
    }
    case "label":
      return `<text class="${n.cls||"lb"}" x="${n.x}" y="${n.y}"${n.anchor?` text-anchor="${n.anchor}"`:""}>${esc(n.text)}</text>`;
  }
  return "";
}
function edgeSvg(e){
  const d="M"+e.pts.map(p=>p[0].toFixed(1)+","+p[1].toFixed(1)).join(" L");
  const mk=e.cls==="lp"?"arrL":"arr";
  let s=`<path class="edge${e.cls?" "+e.cls:""}" d="${d}"${e.a?` marker-end="url(#${mk})"`:""}/>`;
  if(e.lbl) s+=`<text class="${e.cls==="lp"?"lp":"lb"}" x="${e.lx}" y="${e.ly}"${e.la?` text-anchor="${e.la}"`:""}>${esc(e.lbl)}</text>`;
  return s;
}

function buildSection(f){
  const nodes=[],edges=[];
  const headLines=(f.cm||[]).map(t=>({t,c:"cmt"})).concat(f.head.map(t=>({t})));
  const pw=maxW(headLines)+2*PADX+56, ph=headLines.length*LH+2*PADY+10;
  nodes.push({t:"pill",x:-pw/2,y:0,w:pw,h:ph,lines:headLines});
  // 标题信息
  const info = f.q==="__module__"? "模块入口" :
    (f.entry? "入口（未被调用）" : "被调用："+(f.by.join("、")||"-"));
  nodes.push({t:"label",x:-pw/2,y:-10,cls:"tl",text:f.q+"（"+info+"）",anchor:"start"});
  const body=layoutSeq(f.b, f.q);
  body.nodes.forEach(n=>nodes.push(tr(n,0,ph+GAP)));
  body.edges.forEach(e=>edges.push(trE(e,0,ph+GAP)));
  edges.push({pts:[[0,ph],[0,ph+GAP]],a:1});
  let H=ph+GAP+body.h, exit=body.exit;
  if(exit){
    const ew=maxW([{t:"结束"}])+64, eh=38, eTop=H+GAP;
    nodes.push({t:"pill",x:-ew/2,y:eTop,w:ew,h:eh,lines:[{t:f.q==="__module__"?"模块结束":"函数结束"}]});
    edges.push({pts:[[0,H],[0,eTop]],a:1});
    H=eTop+eh;
  }
  let L=Math.max(pw/2,body.L)+10, R=Math.max(pw/2,body.R)+10;
  return {L,R,h:H,nodes,edges};
}

function renderAll(){
  const allNodes=[],allEdges=[]; let y=40; const Ls=[],Rs=[];
  ctx.sections=[];
  DATA.entries.forEach(q=>{
    const f=ctx.funcs[q];
    const sec=buildSection(f);
    sec.nodes.forEach(n=>tr(n,0,y));
    sec.edges.forEach(e=>trE(e,0,y));
    allNodes.push(...sec.nodes); allEdges.push(...sec.edges);
    ctx.sections.push({q,y0:y});
    Ls.push(sec.L); Rs.push(sec.R);
    y+=sec.h+SEC;
  });
  const W=Math.max(...Ls)+Math.max(...Rs)+80, H=y;
  const offX=Math.max(...Ls)+40;
  const svg=`<svg width="${W.toFixed(0)}" height="${H.toFixed(0)}" viewBox="0 0 ${W.toFixed(0)} ${H.toFixed(0)}" xmlns="http://www.w3.org/2000/svg">
<defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#8b95a5"/></marker>
<marker id="arrL" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#6fa8dc"/></marker></defs>
<g transform="translate(${offX},0)">
<g>${allEdges.map(edgeSvg).join("\n")}</g>
<g>${allNodes.map(nodeSvg).join("\n")}</g>
</g></svg>`;
  document.getElementById("cv").innerHTML=svg;
}

/* ============ 交互 ============ */
const view={s:1,tx:0,ty:0};
function applyView(){
  document.getElementById("cv").style.transform=`translate(${view.tx}px,${view.ty}px) scale(${view.s})`;
}
function zoomBy(k){
  const wrap=document.getElementById("wrap");
  const cx=wrap.clientWidth/2, cy=wrap.clientHeight/2;
  const sOld=view.s;
  view.s=Math.min(3,Math.max(0.2,view.s*k));
  const px=(cx-view.tx)/sOld, py=(cy-view.ty)/sOld;
  view.tx=cx-px*view.s; view.ty=cy-py*view.s;
  applyView();
}
function fitWidth(){
  const wrap=document.getElementById("wrap");
  const svg=document.querySelector("#cv svg");
  const W=parseFloat(svg.getAttribute("viewBox").split(" ")[2]);
  view.s=Math.min(1.6,Math.max(0.3,(wrap.clientWidth-40)/W));
  view.tx=(wrap.clientWidth-W*view.s)/2;
  view.ty=24;
  applyView();
}
function resetView(){ view.s=1; view.tx=40; view.ty=24; applyView(); }

document.getElementById("wrap").addEventListener("wheel",e=>{
  e.preventDefault();          // 滚轮直接缩放，平移交给拖拽
  zoomBy(e.deltaY<0?1.12:0.9);
},{passive:false});

/* ============ 鼠标拖拽平移（自由平移，无边界） ============ */
let drag=null, dragMoved=false;
const wrapEl=document.getElementById("wrap");
wrapEl.addEventListener("mousedown",e=>{
  if(e.button!==0) return;
  e.preventDefault();   // 防止拖拽时选中页面文本
  drag={x:e.clientX,y:e.clientY,tx:view.tx,ty:view.ty};
  dragMoved=false;
});
window.addEventListener("mousemove",e=>{
  if(!drag) return;
  const dx=e.clientX-drag.x, dy=e.clientY-drag.y;
  if(Math.abs(dx)+Math.abs(dy)>4){ dragMoved=true; wrapEl.classList.add("dragging"); }
  if(dragMoved){ view.tx=drag.tx+dx; view.ty=drag.ty+dy; applyView(); }
});
window.addEventListener("mouseup",()=>{
  drag=null; wrapEl.classList.remove("dragging");
});

document.getElementById("cv").addEventListener("click",e=>{
  if(dragMoved){ dragMoved=false; return; }   // 拖拽结束后的 click 不算点击
  const g=e.target.closest("g.chip");
  if(!g) return;
  const path=g.dataset.path, fn=g.dataset.fn;
  const key=path+"::"+fn;
  if(ctx.exp.has(key)) ctx.exp.delete(key); else ctx.exp.add(key);
  renderAll();
});

function jumpFn(q){
  const f=ctx.funcs[q]; if(!f) return;
  document.querySelectorAll("#side .fn").forEach(el=>el.classList.toggle("active", el.dataset.q===q));
  if(f.entry){
    const sec=ctx.sections.find(s=>s.q===q);
    if(sec){ view.ty=40-sec.y0*view.s; applyView(); }
    return;
  }
  // 被调用函数：找第一个未展开的调用点，展开并把该标记移到视口中部
  const chips=[...document.querySelectorAll(`g.chip[data-fn="${CSS.escape(q)}"]`)];
  if(!chips.length) return;
  const target=chips.find(c=>!ctx.exp.has(c.dataset.path+"::"+q))||chips[0];
  ctx.exp.add(target.dataset.path+"::"+q);
  const path=target.dataset.path;
  renderAll();
  const el=document.querySelector(`g.chip[data-path="${CSS.escape(path)}"]`);
  if(el){
    const r=el.getBoundingClientRect(), c=wrapEl.getBoundingClientRect();
    view.tx-=(r.left+r.width/2)-(c.left+c.width/2);
    view.ty-=(r.top+r.height/2)-(c.top+c.height/2)-80;
    applyView();
  }
}

function buildIndex(){
  const idx=document.getElementById("idx");
  let html="";
  DATA.functions.forEach(f=>{
    html+=`<span class="fn${f.entry?" entry":""}" data-q="${esc(f.q)}" title="${esc(f.q)}${f.entry?"（入口）":"（被调用："+(f.by.join("、")||"-")+"）"}">${esc(f.q)}${f.entry?'<span class="star">★</span>':""}</span>`;
  });
  idx.innerHTML=html;
  idx.addEventListener("click",e=>{
    const el=e.target.closest(".fn");
    if(el) jumpFn(el.dataset.q);
  });
}

buildIndex();
renderAll();
fitWidth();
</script>
</body>
</html>
"""


def run(target, output):
    data = build_data(target)
    n_funcs = len(data["functions"])
    log(f"分析完成：{n_funcs} 个函数，入口 {len(data['entries'])} 个：{', '.join(data['entries'])}")
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = TEMPLATE.replace("__PAYLOAD__", payload).replace("__FILE__", data["file"])
    with open(output, "w", encoding="utf-8") as f:
        f.write(html)
    log(f"已生成: {output}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python flowgen.py <目标.py> [输出.html]", file=sys.stderr)
        raise SystemExit(1)
    _target = sys.argv[1]
    _output = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_OUTPUT
    try:
        raise SystemExit(run(_target, _output))
    except Exception as e:
        print(f"[错误] {e}", file=sys.stderr)
        raise SystemExit(1)
