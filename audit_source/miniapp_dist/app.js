const API="/ShuiBei/api/miniapp/v1";
const tg=window.Telegram&&window.Telegram.WebApp;
if(tg){try{tg.ready();tg.expand();tg.setHeaderColor("#f4f5f0");tg.setBackgroundColor("#f4f5f0")}catch(e){}}
const initData=tg&&tg.initData?tg.initData:"";
const app=document.getElementById("app");
const now=new Date();
const isoLocal=function(d){const y=d.getFullYear(),m=String(d.getMonth()+1).padStart(2,"0"),day=String(d.getDate()).padStart(2,"0");return y+"-"+m+"-"+day};
const monthStart=new Date(now.getFullYear(),now.getMonth(),1);
const state={viewer:null,categories:["商品","服务","广告","服务器","手续费","人工","其他"],books:[{id:0,name:"主账本"}],book:0,route:"home",peer:0,filter:"all",report:"day",kind:"debt",batch:false,selected:new Set(),customStart:isoLocal(monthStart),customEnd:isoLocal(now)};

function esc(v){return String(v==null?"":v).replace(/[&<>"']/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]})}
function money(v,c){return new Intl.NumberFormat("zh-CN",{maximumFractionDigits:4}).format(Number(v||0)/10000)+" "+(c||"USDT")}
function day(ts){return ts?new Date(ts*1000).toLocaleDateString("zh-CN"):"未设置"}
function idem(){return (crypto&&crypto.randomUUID)?crypto.randomUUID():"sb_"+Date.now()+"_"+Math.random().toString(36).slice(2)}

const pendingFinancialMemory = new Map();
const financialInFlight = new Map();
async function financialReq(path, input) {
  const body = {...input}; delete body.idempotency_key;
  const canonical = JSON.stringify([state.viewer && state.viewer.id || 0, path, body]);
  const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(canonical));
  const fingerprint = Array.from(new Uint8Array(bytes), b => b.toString(16).padStart(2, "0")).join("");
  if (financialInFlight.has(fingerprint)) return financialInFlight.get(fingerprint);
  const storageKey = "sb_pending_financial_v1:" + fingerprint;
  let key = pendingFinancialMemory.get(storageKey) || "";
  try { key = sessionStorage.getItem(storageKey) || key; } catch (_) {}
  if (!key) key = idem();
  pendingFinancialMemory.set(storageKey, key);
  try { sessionStorage.setItem(storageKey, key); } catch (_) {}
  const attempt = (async function() {
    const result = await req(path, "POST", {...body, idempotency_key: key});
    pendingFinancialMemory.delete(storageKey);
    try { sessionStorage.removeItem(storageKey); } catch (_) {}
    return result;
  })();
  financialInFlight.set(fingerprint, attempt);
  try { return await attempt; }
  finally { financialInFlight.delete(fingerprint); }
}

function feedback(kind){try{if(tg&&tg.HapticFeedback){if(kind==="success"||kind==="error")tg.HapticFeedback.notificationOccurred(kind);else tg.HapticFeedback.impactOccurred(kind||"light")}}catch(e){}}
function icon(name){
  const paths={
    home:'<path d="M3 10.5 12 3l9 7.5"/><path d="M5.5 9.5V21h13V9.5"/><path d="M9 21v-7h6v7"/>',
    users:'<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    report:'<path d="M4 19V9"/><path d="M10 19V5"/><path d="M16 19v-7"/><path d="M22 19V3"/><path d="M2 21h22"/>',
    wallet:'<path d="M20 7V5a2 2 0 0 0-2-2H5a3 3 0 0 0 0 6h15v12H5a3 3 0 0 1-3-3V6"/><path d="M16 14h4"/>',
    clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    check:'<path d="m5 12 4 4L19 6"/>',
    arrow:'<path d="m9 18 6-6-6-6"/>',
    search:'<circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/>',
    receipt:'<path d="M6 2h12v20l-3-2-3 2-3-2-3 2V2Z"/><path d="M9 7h6M9 11h6M9 15h4"/>',
    calendar:'<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 10h18"/>',
    bell:'<path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9"/><path d="M10 21h4"/>',
    tag:'<path d="M20 13 11 22 2 13V2h11Z"/><path d="M7 7h.01"/>',
    layers:'<rect x="3" y="3" width="18" height="18" rx="3"/><path d="M8 12h8M12 8v8"/>'
  };
  return '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'+(paths[name]||paths.home)+'</svg>'
}
async function req(path,method,body){
  method=method||"GET";
  const h={Accept:"application/json",Authorization:"tma "+initData};
  if(body!==undefined)h["Content-Type"]="application/json";
  const r=await fetch(API+path,{method:method,headers:h,body:body===undefined?undefined:JSON.stringify(body)});
  let j={};try{j=await r.json()}catch(e){}
  if(!r.ok)throw new Error(j&&j.error&&j.error.message?j.error.message:"请求失败，请稍后重试");
  return j.data
}
function toast(s,type){
  document.querySelectorAll(".toast").forEach(function(x){x.remove()});
  const n=document.createElement("div");n.className="toast "+(type||"");n.textContent=s;document.body.appendChild(n);
  setTimeout(function(){n.classList.add("show")},10);setTimeout(function(){n.classList.remove("show");setTimeout(function(){n.remove()},180)},1800)
}
function ask(title,body,confirmText,danger){
  return new Promise(function(resolve){
    const wrap=document.createElement("div");wrap.className="modal-layer";
    wrap.innerHTML='<div class="modal-card" role="dialog" aria-modal="true"><span class="eyebrow">确认操作</span><h3>'+esc(title)+'</h3><p>'+esc(body)+'</p><div class="modal-actions"><button class="btn secondary" data-cancel>取消</button><button class="btn '+(danger?"danger-btn":"")+'" data-ok>'+esc(confirmText||"确认")+'</button></div></div>';
    document.body.appendChild(wrap);
    const done=function(v){wrap.classList.remove("open");setTimeout(function(){wrap.remove();resolve(v)},150)};
    wrap.querySelector("[data-cancel]").onclick=function(){done(false)};
    wrap.querySelector("[data-ok]").onclick=function(){done(true)};
    wrap.onclick=function(e){if(e.target===wrap)done(false)};
    requestAnimationFrame(function(){wrap.classList.add("open")});
  })
}
function inputAsk(title,body,type,placeholder,value){
  return new Promise(function(resolve){
    const wrap=document.createElement("div");wrap.className="modal-layer";
    wrap.innerHTML='<div class="modal-card" role="dialog" aria-modal="true"><span class="eyebrow">批量操作</span><h3>'+esc(title)+'</h3><p>'+esc(body)+'</p><input class="field" data-value type="'+esc(type||"text")+'" placeholder="'+esc(placeholder||"")+'" value="'+esc(value||"")+'"><div class="modal-actions modal-actions-gap"><button class="btn secondary" data-cancel>取消</button><button class="btn" data-ok>确认</button></div></div>';
    document.body.appendChild(wrap);
    const input=wrap.querySelector("[data-value]");
    const done=function(v){wrap.classList.remove("open");setTimeout(function(){wrap.remove();resolve(v)},150)};
    wrap.querySelector("[data-cancel]").onclick=function(){done(null)};
    wrap.querySelector("[data-ok]").onclick=function(){done(input.value)};
    wrap.onclick=function(e){if(e.target===wrap)done(null)};
    requestAnimationFrame(function(){wrap.classList.add("open");setTimeout(function(){input.focus()},100)});
  })
}
function shell(body,active){
  const name=state.viewer&&state.viewer.name?state.viewer.name:"水杯用户";
  const nav='<nav class="nav" aria-label="主导航">'+
    '<button data-nav="home" class="'+(active==="home"?"active":"")+'">'+icon("home")+'<span>首页</span></button>'+
    '<button data-nav="customers" class="'+((active==="customers"||active==="customer")?"active":"")+'">'+icon("users")+'<span>客户</span></button>'+
    '<button data-nav="reports" class="'+(active==="reports"?"active":"")+'">'+icon("report")+'<span>报表</span></button></nav>';
  return '<div class="app-frame">'+nav+'<main class="shell"><header class="head"><div class="brand"><div class="logo">杯</div><div><strong>水杯记账</strong><span>'+esc(name)+' · 今日经营</span></div></div><div class="head-mark">'+icon("wallet")+'</div></header>'+body+'</main></div>'
}
function skeleton(active){
  return shell('<div class="skeleton hero"></div><div class="skeleton-grid"><div class="skeleton tile"></div><div class="skeleton tile"></div><div class="skeleton tile"></div><div class="skeleton tile"></div></div><div class="skeleton line"></div><div class="skeleton row-sk"></div><div class="skeleton row-sk"></div>',active)
}
function customerRow(x,selectable){
  const debt=x.balance_micro<0,pre=x.balance_micro>0;
  const label=debt?"待收":pre?"预付款":"已结清";
  const amount=debt?x.amount_due_micro:pre?x.prepaid_micro:0;
  const status=x.overdue?'<span class="tag">逾期</span>':(x.due_at&&debt?'<span class="tag neutral">'+day(x.due_at)+'</span>':"");
  const selected=state.selected.has(Number(x.peer_id));
  const selector=selectable?'<span class="pick '+(selected?"selected":"")+'">'+(selected?icon("check"):"")+'</span>':'<div class="avatar">'+esc((x.name||"客").slice(0,1))+'</div>';
  const attr=selectable?'data-select-peer="'+x.peer_id+'"':'data-peer="'+x.peer_id+'"';
  return '<button class="row '+(selectable?"select-row ":"")+(selected?"row-selected":"")+'" '+attr+'>'+selector+'<div class="row-main"><div class="row-title"><strong>'+esc(x.name)+'</strong>'+status+'</div><span class="sub">'+(x.username?"@"+esc(x.username):"往来客户")+'</span></div><div class="amount"><span>'+label+'</span><b class="'+(debt?"danger":pre?"success":"")+'">'+money(amount,x.currency)+'</b></div><span class="row-arrow">'+(selectable?"":icon("arrow"))+'</span></button>'
}
async function viewHome(){
  const d=await req("/home");
  const recent=d.recent_customers.length?d.recent_customers.map(function(x){return customerRow(x,false)}).join(""):'<div class="empty"><div class="empty-icon">'+icon("users")+'</div><strong>还没有客户</strong><span>产生第一笔往来后，会自动出现在这里。</span></div>';
  const attention=(d.overdue_count||d.today_due_count)?'<section class="attention"><div class="attention-title"><div>'+icon("bell")+'<span>今日重点</span></div><small>优先处理到期账款</small></div><div class="attention-actions"><button data-home-filter="overdue"><b class="danger">'+d.overdue_count+'</b><span>已逾期</span></button><button data-home-filter="today"><b>'+d.today_due_count+'</b><span>今日到期</span></button></div></section>':"";
  const profit=d.today_classified_sale_count?money(d.today_gross_profit_micro,d.currency):money(d.prepaid_micro,d.currency);
  const profitLabel=d.today_classified_sale_count?"今日毛利润":"预付款";
  return shell(
    '<section class="overview"><div class="overview-top"><div><span class="eyebrow">当前待收</span><strong>'+money(d.receivable_micro,d.currency)+'</strong><span class="hero-caption">'+d.customer_count+' 位往来客户</span></div><div class="overview-icon">'+icon("wallet")+'</div></div></section>'+
    attention+
    '<section class="metrics"><div class="metric"><div class="metric-icon danger-soft">'+icon("clock")+'</div><div><span>逾期客户</span><strong class="danger">'+d.overdue_count+'</strong></div></div><div class="metric"><div class="metric-icon">'+icon("calendar")+'</div><div><span>今日到期</span><strong>'+d.today_due_count+'</strong></div></div><div class="metric"><div class="metric-icon success-soft">'+icon("check")+'</div><div><span>今日入账</span><strong class="success">'+money(d.today_inflow_micro,d.currency)+'</strong></div></div><div class="metric"><div class="metric-icon">'+icon("report")+'</div><div><span>'+profitLabel+'</span><strong>'+profit+'</strong></div></div></section>'+
    '<div class="title"><div><span class="eyebrow">最近往来</span><h2>客户</h2></div><button data-more>全部客户 '+icon("arrow")+'</button></div><div class="list">'+recent+'</div>',
    "home"
  )
}
function batchDock(){
  if(!state.batch)return "";
  const n=state.selected.size,disabled=n?"":" disabled";
  return '<div class="batch-dock"><div class="batch-count"><b>'+n+'</b><span>已选择</span></div><button data-batch="label"'+disabled+'>'+icon("tag")+'<span>标签</span></button><button data-batch="due"'+disabled+'>'+icon("calendar")+'<span>到期</span></button><button data-batch="remind"'+disabled+'>'+icon("bell")+'<span>催款</span></button><button data-batch="statement"'+disabled+'>'+icon("receipt")+'<span>对账</span></button></div>'
}
function statusText(s){return {posted:"已记账",invoiced:"已开账",partial:"部分结清",settled:"已结清",waived:"已减免",reversed:"已冲正"}[String(s||"posted")]||"已记账"}
function spanDays(sec){const n=Number(sec||0);if(!n)return "暂无";const d=Math.max(1,Math.round(n/86400));return d+" 天"}
function trendChart(data,currency){
  const rows=(data&&data.series)||[];
  if(!rows.length)return '<div class="empty compact"><span>暂无趋势数据。</span></div>';
  const W=640,H=210,P=28,vals=[];
  rows.forEach(function(x){vals.push(Number(x.inflow_micro||0),Number(x.outflow_micro||0),Number(x.gross_profit_micro||0))});
  const max=Math.max.apply(null,vals.concat([1]));
  function pts(key){return rows.map(function(x,i){const xx=P+(W-P*2)*(rows.length===1?0:i/(rows.length-1));const yy=H-P-(H-P*2)*(Number(x[key]||0)/max);return xx.toFixed(1)+","+yy.toFixed(1)}).join(" ")}
  const first=rows[0].date||"",mid=rows[Math.floor((rows.length-1)/2)].date||"",last=rows[rows.length-1].date||"";
  return '<section class="card trend-card"><div class="card-title"><div class="card-icon">'+icon("report")+'</div><div><h3>线性统计图</h3><p>收入、支出与毛利润按天变化</p></div></div><div class="trend-legend"><span class="in">入账</span><span class="out">出账</span><span class="profit">毛利润</span></div><svg class="trend-chart" viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none" aria-label="经营趋势"><line x1="'+P+'" y1="'+(H-P)+'" x2="'+(W-P)+'" y2="'+(H-P)+'" class="axis-line"/><polyline points="'+pts("inflow_micro")+'" class="chart-line in"/><polyline points="'+pts("outflow_micro")+'" class="chart-line out"/><polyline points="'+pts("gross_profit_micro")+'" class="chart-line profit"/></svg><div class="trend-axis"><span>'+esc(first.slice(5))+'</span><span>'+esc(mid.slice(5))+'</span><span>'+esc(last.slice(5))+'</span></div><span class="hint">纵轴自动按当前区间最大值缩放 · 单位 '+esc(currency||"")+'</span></section>'
}
function timelineHtml(rows,currency){
  if(!rows||!rows.length)return '<div class="empty compact"><span>暂无时间轴记录。</span></div>';
  return '<div class="timeline">'+rows.slice(0,12).map(function(x){
    const when=x.created_at?new Date(x.created_at*1000).toLocaleString("zh-CN"):"";
    let title="记录更新",detail="";
    if(x.kind==="ledger"){title=statusText(x.status)+" · "+(x.action==="入"?"入账":x.action==="出"?"出账":x.action);detail=money(x.amount_micro,currency)+(x.remark?" · "+x.remark:"")}
    else if(x.kind==="ledger_reversed"){title="账目冲正";detail="原流水 #"+((x.payload&&x.payload.original_ledger_id)||"")}
    else if(x.kind==="recurring_generated"){title="周期应收已生成";detail=(x.payload&&x.payload.period_key)||""}
    else if(x.kind==="template_applied"){title="已使用记账模板";detail="模板 #"+((x.payload&&x.payload.template_id)||"")}
    else if(x.kind==="recurring_created"){title="已创建周期应收";detail=(x.payload&&x.payload.title)||""}
    return '<div class="timeline-item"><i></i><div><b>'+esc(title)+'</b><span>'+esc(detail)+'</span><small>'+esc(when)+'</small></div></div>'
  }).join("")+'</div>'
}
function customerSummaryHtml(s){
  return '<section class="card"><div class="card-title"><div class="card-icon">'+icon("users")+'</div><div><h3>往来摘要</h3><p>只基于真实账务记录，不做主观信用评分。</p></div></div><div class="summary-grid"><div><span>累计往来</span><b>'+money(s.turnover_micro,s.currency)+'</b></div><div><span>流水</span><b>'+Number(s.record_count||0)+' 笔</b></div><div><span>平均结清</span><b>'+spanDays(s.avg_settlement_seconds)+'</b></div><div><span>最长未结</span><b>'+spanDays(s.longest_unsettled_seconds)+'</b></div><div><span>当前逾期</span><b class="'+(s.overdue_days?"danger":"")+'">'+Number(s.overdue_days||0)+' 天</b></div><div><span>结清次数</span><b>'+Number(s.settlement_count||0)+'</b></div></div></section>'
}

async function viewCustomers(){
  const rows=await req("/customers?filter="+encodeURIComponent(state.filter));
  const fs=[["all","全部"],["debt","待收"],["today","今日到期"],["overdue","已逾期"],["prepay","预付款"]];
  const actions='<button class="batch-toggle '+(state.batch?"active":"")+'" data-batch-toggle>'+icon("layers")+'<span>'+(state.batch?"退出批量":"批量管理")+'</span></button>';
  const selectTools=state.batch?'<div class="select-tools"><span>已选择 '+state.selected.size+' 位</span><div><button data-select-all>全选当前</button><button data-select-clear>清空</button></div></div>':"";
  return shell(
    '<div class="pagehead split-head"><div><span class="eyebrow">往来管理</span><h1>客户</h1><p>先处理待收和到期，再看其它往来。</p></div>'+actions+'</div>'+
    '<form class="search" id="searchForm"><div class="search-field">'+icon("search")+'<input id="searchQ" placeholder="搜索昵称、用户名或标签"></div><button class="btn compact-btn">搜索</button></form>'+
    '<div class="toolbar">'+fs.map(function(x){return '<button type="button" data-filter="'+x[0]+'" class="'+(state.filter===x[0]?"active":"")+'">'+x[1]+'</button>'}).join("")+'</div>'+
    selectTools+
    '<div class="list" id="customerList" data-visible-peers="'+rows.map(function(x){return x.peer_id}).join(",")+'">'+(rows.length?rows.map(function(x){return customerRow(x,state.batch)}).join(""):'<div class="empty"><div class="empty-icon">'+icon("users")+'</div><strong>这里暂时没有客户</strong><span>换个筛选条件看看。</span></div>')+'</div>'+batchDock(),
    "customers"
  )
}
async function viewCustomer(){
  const peer=state.peer;
  const a=await Promise.all([
    req("/customers/"+peer),
    req("/customers/"+peer+"/ledger"),
    req("/customers/"+peer+"/summary"),
    req("/customers/"+peer+"/timeline?limit=30"),
    req("/templates?peer_id="+peer),
    req("/recurring?peer_id="+peer)
  ]);
  const c=a[0],rows=a[1],summary=a[2],timeline=a[3],templates=a[4],recurring=a[5],debt=c.balance_micro<0,pre=c.balance_micro>0;
  const bookMap={};state.books.forEach(function(b){bookMap[Number(b.id)]=b.name});
  const bookOptions=state.books.map(function(b){return '<option value="'+Number(b.id)+'">'+esc(b.name)+'</option>'}).join("");
  let settle="";
  if(debt)settle='<section class="card" id="settleAction"><div class="card-title"><div class="card-icon">'+icon("check")+'</div><div><h3>收款与结清</h3><p>收到款后及时更新余额。</p></div></div><div class="stack"><button class="btn success wide" id="full">收到全款 · '+money(c.amount_due_micro,c.currency)+'</button><div class="two"><input class="field" id="partial" inputmode="decimal" placeholder="部分收款金额"><button class="btn secondary" id="partialBtn">部分收款</button></div><button class="btn danger-outline wide" id="waive">免除当前欠款</button><span class="hint">减免只清除欠款，不计入实际入账。</span></div></section>';
  const ledgerHtml=rows.length?rows.map(function(r){
    let sign=["入","收入","+"].includes(r.action)?"+":["出","支出","-"].includes(r.action)?"-":esc(r.action);
    const meta=[r.category||"",r.cost_micro?"成本 "+money(r.cost_micro,c.currency):"",bookMap[Number(r.book_id||0)]||"主账本",statusText(r.status)].filter(Boolean).join(" · ");
    const canReverse=r.status!=="reversed"&&!r.reversal_of&&["入","收入","+","出","支出","-"].includes(r.action);
    return '<div class="ledger ledger-advanced"><div><strong>'+esc(r.remark||r.action||"账目")+'</strong><span>'+esc(r.time)+(meta?" · "+esc(meta):"")+'</span></div><div class="ledger-side"><b class="'+(sign==="+"?"success":sign==="-"?"danger":"")+'">'+sign+money(r.amount_micro,c.currency)+'</b>'+(canReverse?'<button class="mini-link danger" data-reverse-id="'+r.id+'">冲正</button>':r.reversal_of?'<small>冲正 #'+r.reversal_of+'</small>':"")+'</div></div>'
  }).join(""):'<div class="empty compact"><span>暂无流水。</span></div>';
  const dock='<div class="action-dock"><button data-jump="ledgerAction">'+icon("receipt")+'<span>记账</span></button>'+(debt?'<button data-jump="settleAction">'+icon("check")+'<span>收款</span></button>':"")+'<button data-jump="statementAction">'+icon("report")+'<span>对账</span></button></div>';
  const categoryOptions=state.categories.map(function(x){return '<option value="'+esc(x)+'" '+(x==="其他"?"selected":"")+'>'+esc(x)+'</option>'}).join("");
  const businessFields=state.kind==="debt"?'<div class="two biz-fields"><select class="field" id="category">'+categoryOptions+'</select><input class="field" id="cost" inputmode="decimal" placeholder="成本（可选）"></div><span class="hint">分类用于经营分析；成本不填按 0 计算，只影响利润，不改变客户余额。</span>':"";
  const templateList=templates.length?'<div class="template-list">'+templates.slice(0,8).map(function(t){return '<button class="template-chip" data-template-id="'+t.id+'"><b>'+esc(t.name)+'</b><span>'+money(t.amount_micro,c.currency)+' · '+(t.kind==="debt"?"新增欠款":"收到款")+'</span></button>'}).join("")+'</div>':'<div class="empty compact"><span>还没有记账模板。</span></div>';
  const recurringList=recurring.length?'<div class="recurring-list">'+recurring.slice(0,8).map(function(x){return '<div class="recurring-row"><div><b>'+esc(x.title)+'</b><span>'+money(x.amount_micro,c.currency)+' · '+(x.cadence==="weekly"?"每周":"每月")+' · 下次 '+day(x.next_due_at)+'</span></div><button class="mini-link" data-recurring-toggle="'+x.id+'" data-active="'+(Number(x.active||0)?1:0)+'">'+(Number(x.active||0)?"暂停":"启用")+'</button></div>'}).join("")+'</div>':'<div class="empty compact"><span>还没有周期应收。</span></div>';
  return shell(
    '<div class="pagehead customer-head"><button class="back" data-back aria-label="返回">'+icon("arrow")+'</button><div><span class="eyebrow">客户详情</span><h1>'+esc(c.name)+'</h1><p>'+(c.username?"@"+esc(c.username):"往来客户")+'</p></div></div>'+
    '<section class="customer-balance"><div class="balance-top"><span>'+(debt?"当前待收":pre?"当前预付款":"当前状态")+'</span>'+(c.overdue?'<span class="tag">已逾期</span>':"")+'</div><strong class="'+(debt?"danger":pre?"success":"")+'">'+(debt?money(c.amount_due_micro,c.currency):pre?money(c.prepaid_micro,c.currency):"已结清")+'</strong><p>'+(c.due_at?"到期 "+day(c.due_at):debt?"尚未设置到期日":"账务状态正常")+'</p></section>'+
    customerSummaryHtml(summary)+
    '<div class="customergrid"><div><section class="card" id="ledgerAction"><div class="card-title"><div class="card-icon">'+icon("receipt")+'</div><div><h3>记一笔</h3><p>新增欠款可同时记录项目账、分类和成本。</p></div></div><div class="stack"><div class="seg"><button data-kind="debt" class="'+(state.kind==="debt"?"active":"")+'">新增欠款</button><button data-kind="payment" class="'+(state.kind==="payment"?"active":"")+'">收到款</button></div><input class="field" id="amt" inputmode="decimal" placeholder="金额 '+esc(c.currency)+'"><select class="field" id="book">'+bookOptions+'</select>'+businessFields+'<input class="field" id="remark" placeholder="备注（可选）"><div class="two"><button class="btn wide" id="addLedger">确认记账</button><button class="btn secondary wide" id="saveTemplate">保存模板</button></div></div></section>'+
    '<section class="card"><div class="card-title"><div class="card-icon">'+icon("layers")+'</div><div><h3>记账模板</h3><p>常用金额一键复用。</p></div></div>'+templateList+'</section>'+
    '<section class="card"><div class="card-title"><div class="card-icon">'+icon("calendar")+'</div><div><h3>周期应收</h3><p>到期后自动补生成应收，同一期不会重复。</p></div></div><div class="two"><button class="btn secondary" data-new-recurring="monthly">新增每月</button><button class="btn secondary" data-new-recurring="weekly">新增每周</button></div>'+recurringList+'</section>'+
    '<section class="card" id="dueAction"><div class="card-title"><div class="card-icon">'+icon("calendar")+'</div><div><h3>应收到期</h3><p>用到期日区分今日与逾期客户。</p></div></div><div class="stack"><input class="field" id="due" type="date" value="'+(c.due_at?new Date(c.due_at*1000).toISOString().slice(0,10):"")+'"><div class="two"><button class="btn" id="saveDue">保存日期</button><button class="btn secondary" id="clearDue">清除</button></div><span class="hint">当前：'+(c.overdue?"已逾期 · ":"")+day(c.due_at)+'</span></div></section>'+settle+
    '<section class="card" id="statementAction"><div class="card-title"><div class="card-icon">'+icon("report")+'</div><div><h3>对账单</h3><p>先预览，再决定是否发送。</p></div></div><div class="two"><button class="btn secondary" id="preview">预览</button><button class="btn" id="send" '+(c.has_business?"":"disabled")+'>发送给客户</button></div><div id="statement"></div><span class="hint">'+(c.has_business?"当前 Business 会话可直接发送。":"当前只能预览。")+'</span></section></div>'+
    '<div><section class="card ledger-card"><div class="card-title"><div class="card-icon">'+icon("receipt")+'</div><div><h3>最近流水</h3><p>保留原记录，冲正不会删除历史。</p></div></div>'+ledgerHtml+'</section>'+
    '<section class="card"><div class="card-title"><div class="card-icon">'+icon("clock")+'</div><div><h3>客户时间轴</h3><p>账务、冲正、模板与周期应收统一展示。</p></div></div>'+timelineHtml(timeline,c.currency)+'</section></div></div>'+dock,
    "customer"
  )
}

function categoryBreakdown(d){
  const rows=d.category_breakdown||[];
  if(!rows.length)return '<div class="profit-note">本周期还没有已分类成交；旧流水不会被强行估算利润。</div>';
  return '<section class="card category-card"><div class="card-title"><div class="card-icon">'+icon("tag")+'</div><div><h3>分类表现</h3><p>按已分类成交额排序</p></div></div><div class="category-list">'+rows.map(function(x){return '<div class="category-item"><div><b>'+esc(x.category)+'</b><span>'+x.count+' 笔 · 成本 '+money(x.cost_micro,d.currency)+'</span></div><div><strong>'+money(x.sales_micro,d.currency)+'</strong><span class="'+(x.gross_profit_micro<0?"danger":"success")+'">毛利 '+money(x.gross_profit_micro,d.currency)+'</span></div></div>'}).join("")+'</div></section>'
}
async function viewReports(){
  let summaryPath,trendPath;
  if(state.report==="custom"){
    summaryPath="/reports/custom?start="+encodeURIComponent(state.customStart)+"&end="+encodeURIComponent(state.customEnd);
    trendPath="/reports/trend?start="+encodeURIComponent(state.customStart)+"&end="+encodeURIComponent(state.customEnd)+"&book_id="+encodeURIComponent(state.book);
  }else{
    summaryPath="/reports/"+state.report;
    const days=state.report==="day"?7:state.report==="week"?14:30;
    trendPath="/reports/trend?days="+days+"&book_id="+encodeURIComponent(state.book);
  }
  const all=await Promise.all([req(summaryPath),req(trendPath),req("/books/overview"),req("/snapshots?limit=8")]);
  const d=all[0],trend=all[1],books=all[2],snapshots=all[3];
  const ms=[["day","今日"],["week","本周"],["month","本月"],["custom","自定义"]];
  const net=Number(d.inflow_micro||0)-Number(d.outflow_micro||0);
  const hasProfit=Number(d.classified_sale_count||0)>0;
  const heroValue=hasProfit?Number(d.gross_profit_micro||0):net;
  const heroTitle=hasProfit?"已分类毛利润":"净流入";
  const max=Math.max(Number(d.inflow_micro||0),Number(d.outflow_micro||0),1);
  const inPct=Math.max(3,Math.round(Number(d.inflow_micro||0)/max*100));
  const outPct=Math.max(3,Math.round(Number(d.outflow_micro||0)/max*100));
  const custom=state.report==="custom"?'<form class="range-form" id="rangeForm"><input class="field" type="date" id="rangeStart" value="'+esc(state.customStart)+'"><span>至</span><input class="field" type="date" id="rangeEnd" value="'+esc(state.customEnd)+'"><button class="btn" type="submit">查看</button></form>':"";
  const coverage=Number(d.sale_count||0)?Math.round(Number(d.classified_sale_count||0)/Number(d.sale_count||1)*100):0;
  const bookOptions=state.books.map(function(b){return '<option value="'+Number(b.id)+'" '+(Number(state.book)===Number(b.id)?"selected":"")+'>'+esc(b.name)+'</option>'}).join("");
  const bookRows=books.length?books.map(function(b){return '<div class="category-item"><div><b>'+esc(b.name)+'</b><span>'+Number(b.record_count||0)+' 笔</span></div><div><strong class="'+(Number(b.net_micro||0)<0?"danger":"success")+'">'+(Number(b.net_micro||0)<0?"-":"")+money(Math.abs(Number(b.net_micro||0)),d.currency)+'</strong><span>入 '+money(b.inflow_micro,d.currency)+' · 出 '+money(b.outflow_micro,d.currency)+'</span></div></div>'}).join(""):'<div class="empty compact"><span>暂无项目账数据。</span></div>';
  const snapshotRows=snapshots.length?snapshots.map(function(s){const sm=s.summary||{};return '<div class="snapshot-row"><div><b>'+esc(s.period_key)+'</b><span>'+(s.period_kind==="month"?"月结":"周结")+' · '+new Date(Number(s.created_at||0)*1000).toLocaleDateString("zh-CN")+'</span></div><strong>'+money(sm.gross_profit_micro||0,sm.currency||d.currency)+'</strong></div>'}).join(""):'<div class="empty compact"><span>还没有结账快照。</span></div>';
  return shell(
    '<div class="pagehead"><div><span class="eyebrow">账务概览</span><h1>经营报表</h1><p>现金流、成交成本、毛利润和项目账趋势分开看。</p></div></div>'+
    '<div class="tabs report-tabs">'+ms.map(function(x){return '<button data-report="'+x[0]+'" class="'+(state.report===x[0]?"active":"")+'">'+x[1]+'</button>'}).join("")+'</div>'+custom+
    '<section class="overview report-overview"><div class="overview-top"><div><span class="eyebrow">'+esc(d.title)+' · '+heroTitle+'</span><strong class="'+(heroValue<0?"danger":"success")+'">'+(heroValue<0?"-":"")+money(Math.abs(heroValue),d.currency)+'</strong><span class="hero-caption">'+d.record_count+' 条流水 · '+d.active_customer_count+' 位活跃客户</span></div><div class="overview-icon">'+icon("report")+'</div></div></section>'+
    '<section class="flow-card"><div class="flow-row"><div><span>入账</span><b class="success">'+money(d.inflow_micro,d.currency)+'</b></div><i><em style="width:'+inPct+'%"></em></i></div><div class="flow-row"><div><span>出账 / 新增欠款</span><b>'+money(d.outflow_micro,d.currency)+'</b></div><i><em class="out" style="width:'+outPct+'%"></em></i></div></section>'+
    '<div class="reportgrid"><div class="metric report-metric"><div><span>已分类成交额</span><strong>'+money(d.classified_sales_micro,d.currency)+'</strong></div></div><div class="metric report-metric"><div><span>成本</span><strong>'+money(d.cost_micro,d.currency)+'</strong></div></div><div class="metric report-metric"><div><span>毛利润</span><strong class="'+(Number(d.gross_profit_micro||0)<0?"danger":"success")+'">'+money(d.gross_profit_micro,d.currency)+'</strong></div></div><div class="metric report-metric"><div><span>分类覆盖</span><strong>'+coverage+'%</strong></div></div><div class="metric report-metric"><div><span>当前待收</span><strong class="danger">'+money(d.receivable_micro,d.currency)+'</strong></div></div><div class="metric report-metric"><div><span>逾期客户</span><strong class="danger">'+d.overdue_count+'</strong></div></div></div>'+
    '<div class="trend-select"><label>趋势项目账</label><select class="field" id="trendBook">'+bookOptions+'</select></div>'+trendChart(trend,d.currency)+
    '<section class="card"><div class="card-title split-head"><div class="card-title"><div class="card-icon">'+icon("layers")+'</div><div><h3>项目账</h3><p>主账本之外可按项目归类新流水。</p></div></div><button class="btn secondary compact-btn" id="newBook">新建</button></div><div class="category-list">'+bookRows+'</div></section>'+
    '<section class="card"><div class="card-title"><div class="card-icon">'+icon("calendar")+'</div><div><h3>周 / 月结快照</h3><p>同一周期只保存一次，历史快照不被后续改动覆盖。</p></div></div><div class="two"><button class="btn secondary" data-snapshot="week">保存本周快照</button><button class="btn secondary" data-snapshot="month">保存本月快照</button></div><div class="snapshot-list">'+snapshotRows+'</div></section>'+
    categoryBreakdown(d),
    "reports"
  )
}

async function render(){
  app.innerHTML=skeleton(state.route);
  try{
    const html=state.route==="home"?await viewHome():state.route==="customers"?await viewCustomers():state.route==="customer"?await viewCustomer():await viewReports();
    app.innerHTML=html;bind()
  }catch(e){app.innerHTML=shell('<div class="error"><strong>暂时没加载出来</strong><span>'+esc(e.message||e)+'</span><button onclick="location.reload()">重新加载</button></div>',state.route);bind()}
}
function go(r,p){state.route=r;if(p)state.peer=p;if(r!=="customers"){state.batch=false;state.selected.clear()}feedback("light");window.scrollTo({top:0,behavior:"smooth"});render()}
function jump(id){const el=document.getElementById(id);if(el){feedback("light");el.scrollIntoView({behavior:"smooth",block:"start"})}}
async function runBatch(kind){
  const peers=Array.from(state.selected);
  if(!peers.length)return;
  if(kind==="label"){
    const label=await inputAsk("批量设置标签","将为 "+peers.length+" 位客户设置同一个标签。","text","例如：长期客户","");
    if(label===null)return;
    try{const d=await req("/batch/label","POST",{peer_ids:peers,label:label});feedback("success");toast("已更新 "+d.changed+" 位客户","success");state.selected.clear();render()}catch(e){feedback("error");toast(e.message,"error")}
    return
  }
  if(kind==="due"){
    const value=await inputAsk("批量设置到期日","将为 "+peers.length+" 位客户设置同一个应收到期日。","date","",isoLocal(now));
    if(!value)return;
    const ts=Math.floor(new Date(value+"T23:59:59").getTime()/1000);
    try{const d=await req("/batch/due","POST",{peer_ids:peers,due_at:ts});feedback("success");toast("已更新 "+d.changed+" 位客户","success");state.selected.clear();render()}catch(e){feedback("error");toast(e.message,"error")}
    return
  }
  if(kind==="remind"){
    if(!await ask("批量催款","将尝试向所选 "+peers.length+" 位客户发送催款提醒；没有欠款或 Business 会话不可用的客户会自动跳过。","确认发送",false))return;
    try{const d=await req("/batch/reminders","POST",{peer_ids:peers});feedback("success");toast("已发送 "+d.sent+" 位，跳过 "+d.failed.length+" 位","success");state.selected.clear();render()}catch(e){feedback("error");toast(e.message,"error")}
    return
  }
  if(kind==="statement"){
    if(!await ask("批量发送对账单","将向所选 "+peers.length+" 位客户发送各自的对账单；不可用会话会自动跳过。","确认发送",false))return;
    try{const d=await req("/batch/statements","POST",{peer_ids:peers});feedback("success");toast("已发送 "+d.sent+" 位，跳过 "+d.failed.length+" 位","success");state.selected.clear();render()}catch(e){feedback("error");toast(e.message,"error")}
  }
}
function bind(){
  document.querySelectorAll("[data-nav]").forEach(function(b){b.onclick=function(){go(b.dataset.nav)}});
  document.querySelectorAll("[data-peer]").forEach(function(b){b.onclick=function(){go("customer",Number(b.dataset.peer))}});
  document.querySelectorAll("[data-select-peer]").forEach(function(b){b.onclick=function(){const p=Number(b.dataset.selectPeer);if(state.selected.has(p))state.selected.delete(p);else state.selected.add(p);feedback("light");render()}});
  document.querySelectorAll("[data-jump]").forEach(function(b){b.onclick=function(){jump(b.dataset.jump)}});
  document.querySelectorAll("[data-home-filter]").forEach(function(b){b.onclick=function(){state.filter=b.dataset.homeFilter;go("customers")}});
  document.querySelectorAll("[data-batch]").forEach(function(b){b.onclick=function(){runBatch(b.dataset.batch)}});
  const bt=document.querySelector("[data-batch-toggle]");if(bt)bt.onclick=function(){state.batch=!state.batch;state.selected.clear();feedback("light");render()};
  const sa=document.querySelector("[data-select-all]");if(sa)sa.onclick=function(){const raw=(document.getElementById("customerList").dataset.visiblePeers||"").split(",");raw.forEach(function(x){const p=Number(x);if(p)state.selected.add(p)});render()};
  const sc=document.querySelector("[data-select-clear]");if(sc)sc.onclick=function(){state.selected.clear();render()};
  const more=document.querySelector("[data-more]");if(more)more.onclick=function(){state.filter="all";go("customers")};
  const back=document.querySelector("[data-back]");if(back)back.onclick=function(){go("customers")};
  document.querySelectorAll("[data-filter]").forEach(function(b){b.onclick=function(){state.filter=b.dataset.filter;state.selected.clear();feedback("light");render()}});
  document.querySelectorAll("[data-report]").forEach(function(b){b.onclick=function(){state.report=b.dataset.report;feedback("light");render()}});
  document.querySelectorAll("[data-kind]").forEach(function(b){b.onclick=function(){state.kind=b.dataset.kind;feedback("light");render()}});
  const rf=document.getElementById("rangeForm");if(rf)rf.onsubmit=function(e){e.preventDefault();state.customStart=document.getElementById("rangeStart").value;state.customEnd=document.getElementById("rangeEnd").value;if(!state.customStart||!state.customEnd)return toast("请选择完整日期","error");render()};
  const sf=document.getElementById("searchForm");if(sf)sf.onsubmit=async function(e){e.preventDefault();try{const q=document.getElementById("searchQ").value;const rows=await req("/customers?filter="+encodeURIComponent(state.filter)+"&q="+encodeURIComponent(q));state.selected.clear();document.getElementById("customerList").dataset.visiblePeers=rows.map(function(x){return x.peer_id}).join(",");document.getElementById("customerList").innerHTML=rows.length?rows.map(function(x){return customerRow(x,state.batch)}).join(""):'<div class="empty"><div class="empty-icon">'+icon("search")+'</div><strong>没有找到客户</strong><span>换一个关键词试试。</span></div>';bind()}catch(x){toast(x.message,"error")}};
  const trendBook=document.getElementById("trendBook");if(trendBook)trendBook.onchange=function(){state.book=Number(trendBook.value||0);render()};
  const newBook=document.getElementById("newBook");if(newBook)newBook.onclick=async function(){const name=await inputAsk("新建项目账","给这个项目账起一个名字。","text","例如：服务器项目","");if(!name)return;try{await req("/books","POST",{name:name});state.books=await req("/books");feedback("success");toast("项目账已创建","success");render()}catch(e){feedback("error");toast(e.message,"error")}};
  document.querySelectorAll("[data-snapshot]").forEach(function(b){b.onclick=async function(){try{await req("/snapshots","POST",{period:b.dataset.snapshot});feedback("success");toast("快照已保存","success");render()}catch(e){feedback("error");toast(e.message,"error")}}});
  if(state.route!=="customer")return;
  const peer=state.peer;
  const add=document.getElementById("addLedger");if(add)add.onclick=async function(){if(add.disabled)return;const n=Number(document.getElementById("amt").value);if(!n||n<=0)return toast("请输入正确金额","error");const cat=document.getElementById("category");const costEl=document.getElementById("cost");const bookEl=document.getElementById("book");const cost=costEl&&costEl.value?Number(costEl.value):0;if(cost<0||!Number.isFinite(cost))return toast("请输入正确成本","error");add.disabled=true;try{await financialReq("/customers/"+peer+"/ledger",{kind:state.kind,amount_micro:Math.round(n*10000),remark:document.getElementById("remark").value,category:cat?cat.value:"",cost_micro:Math.round(cost*10000),book_id:Number(bookEl&&bookEl.value||0)});feedback("success");toast("已记账","success");render()}catch(e){feedback("error");toast(e.message,"error");add.disabled=false}};
  const saveTemplate=document.getElementById("saveTemplate");if(saveTemplate)saveTemplate.onclick=async function(){const n=Number(document.getElementById("amt").value);if(!n||n<=0)return toast("先填写模板金额","error");const name=await inputAsk("保存记账模板","保存当前金额、项目账、分类和备注，之后可以一键记账。","text","例如：服务器月费","");if(!name)return;const cat=document.getElementById("category"),costEl=document.getElementById("cost"),bookEl=document.getElementById("book");try{await req("/templates","POST",{name:name,peer_id:peer,kind:state.kind,amount_micro:Math.round(n*10000),remark:document.getElementById("remark").value,category:cat?cat.value:"",cost_micro:Math.round(Number(costEl&&costEl.value||0)*10000),book_id:Number(bookEl&&bookEl.value||0)});feedback("success");toast("模板已保存","success");render()}catch(e){feedback("error");toast(e.message,"error")}};
  document.querySelectorAll("[data-template-id]").forEach(function(b){b.onclick=async function(){if(!await ask("使用记账模板","将立即为当前客户生成一笔账。","确认记账",false))return;try{await req("/templates/"+Number(b.dataset.templateId)+"/apply","POST",{peer_id:peer});feedback("success");toast("模板已应用","success");render()}catch(e){feedback("error");toast(e.message,"error")}}});
  document.querySelectorAll("[data-reverse-id]").forEach(function(b){b.onclick=async function(){if(!await ask("冲正这笔流水","不会删除原记录，而是追加一笔反向流水并标记原账目已冲正。","确认冲正",true))return;try{await req("/ledger/"+Number(b.dataset.reverseId)+"/reverse","POST",{});feedback("success");toast("已冲正","success");render()}catch(e){feedback("error");toast(e.message,"error")}}});
  document.querySelectorAll("[data-new-recurring]").forEach(function(b){b.onclick=async function(){const cadence=b.dataset.newRecurring;const title=await inputAsk("新增周期应收","到期后会幂等生成一笔应收。","text","例如：服务器月费","");if(!title)return;const amount=Number(await inputAsk("周期金额","输入每期应收金额。","number","100",""));if(!amount||amount<=0)return toast("金额无效","error");const date=await inputAsk("首次到期日","选择第一次生成应收的日期。","date","",isoLocal(now));if(!date)return;const bookEl=document.getElementById("book");try{await req("/recurring","POST",{peer_id:peer,title:title,amount_micro:Math.round(amount*10000),cadence:cadence,next_due_at:Math.floor(new Date(date+"T23:59:59").getTime()/1000),book_id:Number(bookEl&&bookEl.value||0),remark:title,category:"服务"});feedback("success");toast("周期应收已创建","success");render()}catch(e){feedback("error");toast(e.message,"error")}}});
  document.querySelectorAll("[data-recurring-toggle]").forEach(function(b){b.onclick=async function(){const active=Number(b.dataset.active||0)?false:true;try{await req("/recurring/"+Number(b.dataset.recurringToggle)+"/active","POST",{active:active});feedback("success");toast(active?"已启用":"已暂停","success");render()}catch(e){feedback("error");toast(e.message,"error")}}});

  async function saveDue(ts){try{await req("/customers/"+peer+"/due","POST",{due_at:ts});feedback("success");toast("到期日已更新","success");render()}catch(e){feedback("error");toast(e.message,"error")}}
  const sd=document.getElementById("saveDue");if(sd)sd.onclick=function(){const v=document.getElementById("due").value;if(!v)return toast("请选择日期","error");saveDue(Math.floor(new Date(v+"T23:59:59").getTime()/1000))};
  const cd=document.getElementById("clearDue");if(cd)cd.onclick=function(){saveDue(0)};
  const full=document.getElementById("full");if(full)full.onclick=async function(){if(!await ask("确认收到全款","这会把当前欠款直接结清。","确认收款",false))return;full.disabled=true;try{await financialReq("/customers/"+peer+"/settle",{mode:"full"});feedback("success");toast("已结清","success");render()}catch(e){feedback("error");toast(e.message,"error");full.disabled=false}};
  const pb=document.getElementById("partialBtn");if(pb)pb.onclick=async function(){if(pb.disabled)return;const n=Number(document.getElementById("partial").value);if(!n||n<=0)return toast("请输入部分收款金额","error");pb.disabled=true;try{await financialReq("/customers/"+peer+"/settle",{mode:"partial",amount_micro:Math.round(n*10000)});feedback("success");toast("已记部分收款","success");render()}catch(e){feedback("error");toast(e.message,"error");pb.disabled=false}};
  const waive=document.getElementById("waive");if(waive)waive.onclick=async function(){if(!await ask("免除当前欠款","减免不会计入实际收款，确认后会清除当前欠款。","确认减免",true))return;waive.disabled=true;try{await financialReq("/customers/"+peer+"/settle",{mode:"waive"});feedback("success");toast("欠款已减免","success");render()}catch(e){feedback("error");toast(e.message,"error");waive.disabled=false}};
  async function statement(send){try{const d=await req("/customers/"+peer+"/statement","POST",{send:send});if(send){feedback("success");toast("对账单已发送","success")}else document.getElementById("statement").innerHTML='<div class="statement">'+esc(d.html.replace(/<[^>]+>/g,""))+'</div>'}catch(e){feedback("error");toast(e.message,"error")}}
  const pv=document.getElementById("preview");if(pv)pv.onclick=function(){statement(false)};
  const sn=document.getElementById("send");if(sn)sn.onclick=async function(){sn.disabled=true;try{await statement(true)}finally{sn.disabled=false}};
}
async function start(){
  if(!initData){app.innerHTML='<div class="boot"><div class="logo large">杯</div><strong>请从 Telegram 打开水杯记账</strong><span>打开后即可安全查看你的账本。</span></div>';return}
  try{const b=await req("/bootstrap");state.viewer=b.viewer;if(Array.isArray(b.categories)&&b.categories.length)state.categories=b.categories;if(Array.isArray(b.books)&&b.books.length)state.books=b.books;render()}catch(e){app.innerHTML='<div class="boot"><div class="logo large">杯</div><strong>暂时没打开</strong><span>'+esc(e.message)+'</span><button class="btn" onclick="location.reload()">重新加载</button></div>'}
}
start();
