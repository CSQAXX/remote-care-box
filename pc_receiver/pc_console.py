#!/usr/bin/env python
"""
远程看护盒子 · PC 端简易控制台（单文件，零第三方依赖）

启动：
    python pc_console.py                       # 默认 192.168.2.138
    python pc_console.py --device 192.168.2.1
    python pc_console.py --port 8899

然后浏览器打开 http://127.0.0.1:8899

提供：
  ① 今天动了没有  —— 最近活动时间、今日活动次数、活动曲线
  ② 有事没事    —— 求助事件 / 设备在线 / 音频就绪
  ③ 点一下就能看 —— 实时画面 + 按住说话对讲
  ④ AI 对话      —— 语言大模型语音对话（chat_bridge.py，可选功能）
  ⑤ 模式切换     —— 看护固件 ⇄ 小智 AI 固件（mode_switch.py，走 USB 烧录）

用到的板子接口（固件已全部实现）：
  GET  /stream        MJPEG 视频
  GET  /event         待处理事件（求助按键）
  GET  /health        设备状态 JSON
  GET  /activity      活动状态 JSON
  GET  /audio/alaw    上行 G.711 A-law 16k 单声道
  POST /audio/down    下行 PCM16 16k 单声道（立即播放）
  POST /notify        告诉板子"我收到了"
"""
import argparse
import io
import json
import mimetypes
import os
import socket
import struct
import threading
import time
import wave
import wsgiref.simple_server
from wsgiref.simple_server import make_server
from http.server import BaseHTTPRequestHandler

try:
    import audioop
except ImportError:      # Python 3.13+ 移除了 audioop
    audioop = None

DEFAULT_DEVICE = "192.168.2.138"
DEVICE_PORT = 8080          # 数据面: /stream /raw /audio/alaw /audio/down
CONTROL_PORT = 8081        # 控制面: /health /activity /event /notify /cmd
ALAW_SAMPLE_RATE = 16000
PCM_SAMPLE_RATE = 16000

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>远程看护盒子 · 控制台</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, "Microsoft YaHei", sans-serif;
         margin: 0; background: #f5f6f8; color: #1f2328; }
  header { background: #2563eb; color: #fff; padding: 12px 20px;
           display: flex; align-items: center; gap: 12px; }
  header h1 { font-size: 18px; margin: 0; font-weight: 600; }
  .dot { width: 10px; height: 10px; border-radius: 50%; background: #c00; }
  .dot.on { background: #16a34a; box-shadow: 0 0 8px #16a34a; }
  .wrap { max-width: 1100px; margin: 0 auto; padding: 16px; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px,1fr));
           gap: 14px; }
  .card { background: #fff; border-radius: 12px; padding: 16px 18px;
          box-shadow: 0 1px 3px rgba(0,0,0,.08); }
  .card h3 { margin: 0 0 12px; font-size: 15px; color: #57606a;
             font-weight: 600; }
  .big { font-size: 26px; font-weight: 700; margin: 4px 0; }
  .ok { color: #16a34a; } .warn { color: #d97706; } .bad { color: #dc2626; }
  .muted { color: #6b7280; font-size: 13px; }
  #view { width: 100%; background: #000; border-radius: 10px; display: block; }
  .bar { height: 8px; background: #e5e7eb; border-radius: 4px; overflow: hidden;
         margin-top: 8px; }
  .bar > i { display: block; height: 100%; background: #2563eb; width: 0; }
  button { font: inherit; border: none; border-radius: 8px; padding: 10px 18px;
           cursor: pointer; background: #2563eb; color: #fff; font-weight: 600; }
  button.ghost { background: #e5e7eb; color: #1f2328; }
  button.talk { background: #dc2626; }
  button.talk.active { background: #16a34a; }
  .row { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 12px; }
  #alert { display: none; background: #fee2e2; border: 2px solid #dc2626;
           border-radius: 10px; padding: 16px; margin-bottom: 14px;
           font-size: 18px; font-weight: 700; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  td { padding: 4px 0; border-bottom: 1px solid #f0f0f0; }
  td:last-child { text-align: right; font-variant-numeric: tabular-nums; }
  .tag { display: inline-block; padding: 1px 8px; border-radius: 20px;
         font-size: 12px; font-weight: 600; }
  .tag.g { background: #dcfce7; color: #166534; }
  .tag.r { background: #fee2e2; color: #991b1b; }
</style>
</head>
<body>
<header>
  <span class="dot" id="dot"></span>
  <h1>远程看护盒子 · 控制台</h1>
  <span class="muted" id="hdrInfo" style="color:#dbeafe"></span>
</header>

<div class="wrap">
  <div id="alert"></div>

  <div class="cards">
    <!-- ① 今天动了没有 -->
    <div class="card">
      <h3>① 今天动了没有</h3>
      <div class="big" id="lastActive">--</div>
      <div class="muted" id="actCount">读取中...</div>
      <div class="bar"><i id="actBar"></i></div>
      <div class="muted" id="actDetail" style="margin-top:10px"></div>
    </div>

    <!-- ② 有事没事 -->
    <div class="card">
      <h3>② 有事没事</h3>
      <div class="big ok" id="statusTitle">--</div>
      <table style="margin-top:8px">
        <tr><td>设备在线</td><td><span class="tag g" id="tOnline">--</span></td></tr>
        <tr><td>摄像头</td><td><span class="tag g" id="tCam">--</span></td></tr>
        <tr><td>音频链路</td><td><span class="tag g" id="tAudio">--</span></td></tr>
        <tr><td>活动检测</td><td><span class="tag g" id="tAct">--</span></td></tr>
        <tr><td>网络推送</td><td><span class="tag g" id="tPush">--</span></td></tr>
        <tr><td>供电方式</td><td><span class="tag g" id="tPower">--</span></td></tr>
        <tr><td>电池电量</td><td id="tBattery">--</td></tr>
        <tr><td>运行时长</td><td id="tUptime">--</td></tr>
      </table>
    </div>

    <!-- ③ 点一下就能看 -->
    <div class="card">
      <h3>③ 点一下就能看</h3>
      <img id="view" src="/view/stream" alt="实时画面">
      <div class="row">
        <button class="talk" id="btnTalk">按住说话</button>
        <button class="ghost" id="btnRec">录一段老人语音</button>
        <button class="ghost" id="btnReload">刷新画面</button>
      </div>
      <div class="muted" id="talkHint" style="margin-top:8px">
        按住「按住说话」→ 电脑麦克风 → 板子喇叭；松手停止。
      </div>
    </div>

    <!-- ④ AI 对话 -->
    <div class="card">
      <h3>④ AI 对话（语言大模型）</h3>
      <div class="row" style="margin-top:0">
        <button id="btnChat">开启 AI 对话</button>
        <button class="ghost" id="btnChatSay">文字问一句</button>
      </div>
      <div class="muted" id="chatState" style="margin-top:10px">未开启</div>
      <div class="muted" id="chatProblems" style="color:#b45309;margin-top:6px"></div>
      <input id="chatText" type="text" placeholder="输入一句话，让盒子念给你听"
             style="width:100%;margin-top:10px;padding:9px 10px;border:1px solid #d0d7de;
                    border-radius:8px;font:inherit;box-sizing:border-box">
      <div id="chatLog" style="margin-top:10px;max-height:170px;overflow:auto;
           font-size:13px;line-height:1.7"></div>
    </div>

    <!-- ⑤ 模式切换：看护固件 ⇄ 小智 AI 固件 -->
    <div class="card" style="grid-column:1/-1">
      <h3>⑤ 模式切换（看护 ⇄ 小智 AI）</h3>      <div class="row" style="margin-top:0;align-items:center">
        <div class="big" id="modeNow" style="font-size:20px">检测中…</div>
        <button id="btnMode">切换模式</button>
      </div>
      <div class="muted" id="modeHint" style="margin-top:8px">
        按一下切到下一个模式，再按一下继续切。三种模式轮流：
        <b>看护</b> → <b>看护+AI对话</b>（唤醒词「晴老师」+ 云端大模型）→
        <b>小智 AI</b> → 回到看护。切换走 USB 串口烧录，约 1–2 分钟，
        期间板子会重启，画面 / 对讲 / 对话会中断；Wi-Fi 和音量会自动保留，不用重新配网。
        切换完刷新一下本页即可。
      </div>
      <div class="muted" id="modeState" style="margin-top:6px"></div>
      <div id="modeLog" style="margin-top:8px;max-height:110px;overflow:auto;
           font-size:12px;line-height:1.6;color:#6b7280;white-space:pre-wrap"></div>
    </div>

    <!-- ⑥ 板端 AI 对话（唤醒词「晴老师」+ 云端大模型） -->
    <div class="card" style="grid-column:1/-1">
      <h3>⑥ 板端 AI 对话（唤醒词 + 云端大模型）</h3>
      <div class="row" style="margin-top:0;align-items:center">
        <div class="big" id="aiState" style="font-size:20px">--</div>
        <span class="muted" id="aiWakeName"></span>
      </div>
      <table style="margin-top:8px">
        <tr><td>唤醒词</td><td><span class="tag g" id="tWake">--</span></td>
            <td>命中次数</td><td id="tWakeHits">--</td></tr>
        <tr><td>最近概率</td><td id="tWakeProb">--</td>
            <td>服务器</td><td><span class="tag g" id="tAiConn">--</span></td></tr>
        <tr><td>对话轮次</td><td id="tAiTurns">--</td>
            <td>错误数</td><td id="tAiErrs">--</td></tr>
      </table>
      <div class="muted" style="margin-top:10px">
        老人对盒子说「<b id="aiWakeWord">晴老师</b>」→ 盒子离线识别唤醒 →
        接着说话 → 音频直传云端大模型 → 喇叭播回回复。全程不经本电脑。
      </div>
      <div class="muted" style="margin-top:6px" id="aiUser"></div>
      <div class="muted" style="margin-top:4px;color:#2563eb" id="aiReply"></div>
    </div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);

function fmtDur(sec) {
  sec = Math.floor(sec || 0);
  const d = Math.floor(sec/86400), h = Math.floor(sec%86400/3600),
        m = Math.floor(sec%3600/60), s = sec%60;
  if (d) return d + " 天 " + h + " 小时";
  if (h) return h + " 小时 " + m + " 分";
  if (m) return m + " 分 " + s + " 秒";
  return s + " 秒";
}
function fmtAgo(ms) {
  if (!ms) return "暂无记录";
  const s = (Date.now() - ms) / 1000;
  if (s < 0) return "刚刚";
  return fmtDur(s) + "前";
}
function setTag(el, ok, okText, badText) {
  el.textContent = ok ? okText : badText;
  el.className = "tag " + (ok ? "g" : "r");
}

// ---------- 告警弹窗 ----------
let lastEventSeq = null;
async function pollEvent() {
  try {
    const r = await fetch('/api/event?since=' + (lastEventSeq ?? ''), {cache:'no-store'});
    const d = await r.json();
    $('dot').className = 'dot on';
    if (d.type === 'button' && d.seq !== lastEventSeq) {
      lastEventSeq = d.seq;
      const box = $('alert');
      box.textContent = d.msg || '老人按了求助按钮';
      box.style.display = 'block';
      fetch('/api/notify', {method:'POST', body:d.msg || ''});
      setTimeout(() => box.style.display = 'none', 10000);
    }
  } catch (e) {
    $('dot').className = 'dot';
  }
}

// ---------- 状态轮询 ----------
async function pollHealth() {
  try {
    const h = await (await fetch('/api/health', {cache:'no-store'})).json();
    $('dot').className = 'dot on';
    $('hdrInfo').textContent = '设备 ' + (h.ip || '-') + ' · RSSI ' + (h.rssi ?? '-') + "dBm" +
      (h.power_source ? ' · ' + (h.power_source === 'typec' ? 'Type-C供电' : '电池供电') : '');

    setTag($('tOnline'), h.camera_ok !== undefined, '在线', '异常');
    setTag($('tCam'), h.camera_ok === true, '正常', '故障');
    setTag($('tAudio'), h.audio_ready === true, '就绪', '未就绪');
    setTag($('tAct'), h.cam_frames > 0, '运行中', '未运行');
    setTag($('tPush'), h.push_connected === true, '已连接', '未连接');
    $('tUptime').textContent = fmtDur(h.uptime);

    /*
     * 供电状态。
     *
     * 为什么单独显示:
     *   "拔掉 Type-C 就没画面" 的根因是板子彻底断电 —— AW9523B 一死,
     *   摄像头/音频/背光的电源轨全断, 屏幕必然全灭。
     *   所以供电方式是判断"设备为什么没画面"的第一现场信息。
     *
     * 注意: 没装电池时, 板子的供电来源也会报成"电池"(充电芯片不会
     *       拉低充电检测脚), 所以还要看 battery_valid 和电压。
     */
    const psrc = h.power_source || 'unknown';
    if (psrc === 'typec') {
      setTag($('tPower'), true, 'Type-C', '未知');
    } else if (psrc === 'battery') {
      setTag($('tPower'), true, '电池供电', '未知');
    } else {
      setTag($('tPower'), false, '未知', '未知');
    }

    if (h.battery_valid === true) {
      const mv = h.battery_mv ?? 0;
      const pct = h.battery_pct ?? 0;
      $('tBattery').textContent = pct + '% (' + mv + 'mV)' +
        (h.charging ? ' · 充电中' : '');
      // 电量低于 20% 用醒目色, 方便一眼看出快没电了
      $('tBattery').className = pct <= 20 ? 'warn' : '';
    } else {
      $('tBattery').textContent = '未读到（可能没装电池）';
      $('tBattery').className = 'muted';
    }

    const bad = !(h.camera_ok && h.audio_ready);
    const st = $('statusTitle');
    if (bad) { st.textContent = '设备异常'; st.className = 'big bad'; }
    else if (psrc === 'battery') { st.textContent = '电池供电中'; st.className = 'big warn'; }
    else if (h.push_connected === false) { st.textContent = '设备正常，推送未连'; st.className = 'big warn'; }
    else { st.textContent = '无异常'; st.className = 'big ok'; }

    updateAiCard(h);
  } catch (e) {
    $('dot').className = 'dot';
    $('statusTitle').textContent = '设备离线';
    $('statusTitle').className = 'big bad';
    $('aiState').textContent = '设备离线';
    $('aiState').className = 'big bad';
  }
}

/*
 * ⑥ 板端 AI 对话卡片。
 *
 * 数据全部来自板子 /health 里的 ai_* / wake_* 字段:
 *   ai_state      当前状态 (空闲/聆听中/思考中/播放中/出错)
 *   ai_connected  是否连着云端 AI 网关
 *   ai_turns      累计对话轮次
 *   ai_replies    累计成功回复
 *   ai_errors     累计错误
 *   ai_last_user  最近一次识别到的老人说话
 *   ai_last_reply 最近一次大模型回复
 *   wake_ready    唤醒词模型是否加载成功
 *   wake_hits     唤醒命中次数
 *   wake_prob     最近一次命中概率
 *   wake_name     唤醒词名字
 *
 * 老固件没有 wake_* 字段 -> 显示"该固件未启用唤醒词", 不报错。
 */
function updateAiCard(h) {
  const hasAi = (h.ai_state !== undefined);
  if (!hasAi) {
    $('aiState').textContent = '本固件未启用 AI 对话';
    $('aiState').className = 'big warn';
    $('tWake').textContent = '未启用';
    $('tWake').className = 'tag r';
    $('aiWakeName').textContent = '';
    return;
  }

  const states = {
    '空闲': '空闲', '聆听中': '聆听中', '思考中': '思考中',
    '播放中': '播放中', '出错': '出错'
  };
  const sname = states[h.ai_state] || h.ai_state;
  const si = $('aiState');
  si.textContent = sname;
  si.className = 'big ' + (h.ai_state === '出错' ? 'bad'
                        : (h.ai_state === '空闲' ? '' : 'ok'));

  $('aiWakeName').textContent = h.wake_name ? ('唤醒词：' + h.wake_name) : '';
  if (h.wake_name) $('aiWakeWord').textContent = h.wake_name;

  if (h.wake_ready !== undefined) {
    setTag($('tWake'), h.wake_ready === true, '就绪', '未就绪');
    $('tWakeHits').textContent = (h.wake_hits ?? 0) + ' 次';
    $('tWakeProb').textContent = (h.wake_prob !== undefined && h.wake_prob > 0)
      ? Number(h.wake_prob).toFixed(3) : '尚无命中';
  } else {
    setTag($('tWake'), false, '未启用', '未启用');
    $('tWakeHits').textContent = '-';
    $('tWakeProb').textContent = '-';
  }

  setTag($('tAiConn'), h.ai_connected === true, '已连接', '未连接');
  $('tAiTurns').textContent = (h.ai_turns ?? 0) + ' 轮';
  $('tAiErrs').textContent = (h.ai_errors ?? 0) + ' 次';

  $('aiUser').textContent = h.ai_last_user ? ('老人说：' + h.ai_last_user) : '';
  $('aiReply').textContent = h.ai_last_reply ? ('大模型：' + h.ai_last_reply) : '';
}

async function pollActivity() {
  try {
    const a = await (await fetch('/api/activity', {cache:'no-store'})).json();
    if (a.error) { $('actCount').textContent = '活动检测未初始化'; return; }
    const last = a.last_active_ts || 0;
    $('lastActive').textContent = last ? fmtAgo(last) : '暂无活动';
    $('actCount').textContent = '今日活动 ' + (a.count ?? a.today_count ?? '-') + ' 次';
    const pct = Math.min(100, (a.count ?? 0) / 6 * 100);
    $('actBar').style.width = pct + '%';
    $('actDetail').textContent =
      '人形在场: ' + (a.human_present ? '是' : '否') +
      ' · 有运动: ' + (a.motion ? '是' : '否') +
      (a.low_light ? ' · 光线不足(不判异常)' : '');
  } catch (e) {}
}

// ---------- 对讲 ----------
/* 用 AudioContext 直接抓原始 Float32 PCM, 转 PCM16 上传。
   不用 MediaRecorder: 浏览器输出 webm/opus, 服务端解不开 (本机无 ffmpeg)。 */
const btnTalk = $('btnTalk');
let talking = false, audioCtx = null, procNode = null, srcNode = null, micStream = null;

function f32ToPcm16(f32) {
  const n = f32.length;
  const buf = new ArrayBuffer(n * 2);
  const dv = new DataView(buf);
  for (let i = 0; i < n; i++) {
    let s = Math.max(-1, Math.min(1, f32[i]));
    dv.setInt16(i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return buf;
}

async function startTalk() {
  if (talking) return;
  try {
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: {channelCount: 1, echoCancellation: true, noiseSuppression: true}});
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    srcNode = audioCtx.createMediaStreamSource(micStream);
    procNode = audioCtx.createScriptProcessor(4096, 1, 1);
    procNode.onaudioprocess = e => {
      if (!talking) return;
      const pcm = f32ToPcm16(e.inputBuffer.getChannelData(0));
      fetch('/api/talk', {method: 'POST', body: pcm,
        headers: {'Content-Type': 'application/octet-stream'}}).catch(() => {});
    };
    srcNode.connect(procNode);
    procNode.connect(audioCtx.destination);   // 必须接到 destination 才会触发 onaudioprocess
    talking = true;
    btnTalk.classList.add('active');
    btnTalk.textContent = '正在说话… 松开停止';
  } catch (e) {
    $('talkHint').textContent = '麦克风不可用: ' + e.message;
  }
}
function stopTalk() {
  if (!talking) return;
  talking = false;
  try { if (procNode) procNode.disconnect(); } catch (e) {}
  try { if (srcNode) srcNode.disconnect(); } catch (e) {}
  try { if (micStream) micStream.getTracks().forEach(t => t.stop()); } catch (e) {}
  if (audioCtx) { audioCtx.close(); audioCtx = null; }
  btnTalk.classList.remove('active');
  btnTalk.textContent = '按住说话';
}
btnTalk.addEventListener('mousedown', startTalk);
btnTalk.addEventListener('mouseup', stopTalk);
btnTalk.addEventListener('mouseleave', stopTalk);
btnTalk.addEventListener('touchstart', e => { e.preventDefault(); startTalk(); });
btnTalk.addEventListener('touchend', e => { e.preventDefault(); stopTalk(); });

// ---------- 录一段老人语音 (上行) ----------
$('btnRec').addEventListener('click', async () => {
  const btn = $('btnRec');
  btn.textContent = '录音中… 点此停止';
  btn.disabled = true;
  try {
    const resp = await fetch('/api/record');
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = new Audio(url);
    a.play();
    $('talkHint').textContent = '已录到 ' + (blob.size/1024).toFixed(1) + ' KB 老人语音，正在播放…';
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  } catch (e) {
    $('talkHint').textContent = '录音失败: ' + e.message;
  }
  btn.textContent = '录一段老人语音';
  btn.disabled = false;
});

$('btnReload').addEventListener('click', () => {
  $('view').src = '/view/stream?t=' + Date.now();
});

// ---------- ④ AI 对话 ----------
/* 对话桥在 PC 端后台线程里跑：拉 /audio/alaw -> VAD -> ASR -> LLM -> TTS
   -> POST /audio/down。这里只负责开关、状态展示和文字调试。 */
let chatOn = false, chatBusy = false;
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}
async function pollChat() {
  try {
    const s = await (await fetch('/api/chat/status', {cache:'no-store'})).json();
    chatOn = !!s.running;
    const map = {idle:'未开启', listening:'正在听', thinking:'正在想', speaking:'正在说',
                 error:'出错'};
    const st = (map[s.state] || s.state || '未知');
    const cnt = (s.stats && s.stats.replies) ? s.stats.replies : 0;
    $('chatState').textContent = st + (s.detail ? ' · ' + s.detail : '') +
      ' · 后端 ' + (s.backend || '-') + (cnt ? ' · 已对话 ' + cnt + ' 轮' : '');
    $('btnChat').textContent = chatOn ? '关闭 AI 对话' : '开启 AI 对话';
    $('btnChat').className = chatOn ? 'talk active' : '';
    const p = s.config_problems || [];
    let msg = p.length ? ('配置待补：' + p.join('；')) : '';
    if (s.error) msg += (msg ? '；' : '') + '最近错误：' + s.error;
    $('chatProblems').textContent = msg;
    let html = '';
    for (const r of (s.rounds || []).slice().reverse()) {
      html += '<div style="margin-bottom:6px">' +
        '<span style="color:#2563eb;font-weight:600">' + esc(r.user || '(语音)') + '</span><br>' +
        '<span style="color:#374151">' + esc(r.reply || '(小智服务端语音回复)') + '</span>' +
        '<span class="muted"> · ' + esc(r.ts) + '</span></div>';
    }
    $('chatLog').innerHTML = html || '<span class="muted">还没有对话记录</span>';
  } catch (e) {
    $('chatState').textContent = '对话桥未加载：' + e.message;
  }
}
$('btnChat').addEventListener('click', async () => {
  if (chatBusy) return;
  chatBusy = true; $('btnChat').disabled = true;
  try {
    const r = await (await fetch('/api/chat/' + (chatOn ? 'stop' : 'start'),
      {method:'POST'})).json();
    if (!r.ok) alert((r.msg || '操作失败') + '\n' + (r.problems || []).join('\n'));
    await pollChat();
  } finally { chatBusy = false; $('btnChat').disabled = false; }
});
$('btnChatSay').addEventListener('click', async () => {
  const t = $('chatText').value.trim();
  if (!t) { $('chatText').focus(); return; }
  const r = await (await fetch('/api/chat/say', {method:'POST', body:t})).json();
  if (!r.ok) alert((r.msg || '提交失败') + '\n' + (r.problems || []).join('\n'));
  else $('chatText').value = '';
  await pollChat();
});
$('chatText').addEventListener('keydown', e => {
  if (e.key === 'Enter') $('btnChatSay').click();
});

// ---------- ⑤ 模式切换 ----------
/* 两套固件轮流跑：后端用 esptool 走 USB 整片烧录，不依赖板子 IP。
   这里只负责显示当前模式、按一下发起切换、轮询进度与结果。 */
let modeBusy = false;
let modeArmed = 0;        // 0=未确认；非 0=已点过一次，等第二次确认
let modeArmTimer = null;
async function pollMode() {
  try {
    const s = await (await fetch('/api/mode/status', {cache:'no-store'})).json();
    modeBusy = !!s.busy;
    const nextName = s.next === 'xiaozhi' ? '切成小智 AI'
                   : (s.next === 'kanhu-ai' ? '切成看护+AI对话' : '回到看护模式');
    $('modeNow').textContent = s.busy
      ? ('切换中 → ' + (s.target === 'xiaozhi' ? '小智 AI'
                       : (s.target === 'kanhu-ai' ? '看护+AI对话' : '看护模式')))
      : (s.mode_name || '检测中');
    $('modeNow').className = s.busy ? 'big warn' : 'big';
    const btn = $('btnMode');
    if (modeArmed && !s.busy) {
      // 已进入二次确认：保持提示，别被轮询刷掉
      btn.disabled = false;
      btn.textContent = '再点一下 = 开始切换';
    } else {
      btn.disabled = s.busy || !s.ready;
      btn.textContent = s.busy ? ('切换中… ' + s.elapsed + ' 秒') : nextName;
    }
    let msg = s.busy ? (s.phase + ' · 请勿断电或拔 USB') : (s.last_result || '');
    if (!s.ready) msg += ' ⚠ 工具未就绪：' + (s.problems || []).join('；');
    if (!modeArmed) $('modeState').textContent = msg;
    $('modeLog').textContent = (s.log || []).slice(-6).join('\n');
  } catch (e) {
    $('modeState').textContent = '模式切换模块未加载：' + e.message;
  }
}
$('btnMode').addEventListener('click', async () => {
  if (modeBusy) return;
  const btn = $('btnMode');
  // 两步确认：不用 confirm()/alert() —— 内置预览面板会拦掉系统弹窗，
  // confirm() 直接返回 false，表现为"点了没反应"。
  if (!modeArmed) {
    modeArmed = Date.now();
    btn.textContent = '再点一下 = 开始切换';
    $('modeState').textContent = '已收到点击：6 秒内再点一次即开始烧录（板子会重启 1–2 分钟）';
    if (modeArmTimer) clearTimeout(modeArmTimer);
    modeArmTimer = setTimeout(() => {
      modeArmed = 0; modeArmTimer = null;
      $('modeState').textContent = '已取消：超时未再次确认';
      pollMode();
    }, 6000);
    return;
  }
  modeArmed = 0;
  if (modeArmTimer) { clearTimeout(modeArmTimer); modeArmTimer = null; }
  modeBusy = true; btn.disabled = true;
  $('modeState').textContent = '正在启动切换…';
  $('modeLog').textContent = '已提交，请勿断电 / 拔 USB…';
  try {
    const r = await (await fetch('/api/mode/switch', {method:'POST'})).json();
    if (!r.ok) {
      $('modeState').textContent = '切换失败：' + (r.msg || '未知错误');
      $('modeLog').textContent = (r.problems || []).join('\n') || (r.msg || '');
    } else if (r.busy) {
      $('modeState').textContent = '切换中 · 请勿断电或拔 USB';
    }
  } catch (e) {
    $('modeState').textContent = '请求失败：' + e.message;
  } finally {
    modeBusy = false;
    await pollMode();
  }
});

// 页面内报错可见，免得 JS 异常时"点了没反应"
window.addEventListener('error', ev => {
  const box = $('modeState');
  if (box) box.textContent = '页面脚本报错：' + (ev.message || '');
});

pollEvent(); pollHealth(); pollActivity(); pollChat(); pollMode();
setInterval(pollEvent, 700);
setInterval(pollHealth, 2000);
setInterval(pollActivity, 5000);
setInterval(pollChat, 1500);
setInterval(pollMode, 1500);
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------
# 板子侧 HTTP 代理
# --------------------------------------------------------------------------
#
# 【连接复用】为什么要自己写池, 不用 urllib
#
#   urllib 每次请求都新建 TCP 连接, 用完就 close -> 走的是 TIME_WAIT。
#   控制台每 700ms 轮询一次事件、每 2s 一次健康检查, 再加上浏览器
#   轮询, 短短几分钟就能在 PC 上堆出几百个 TIME_WAIT:
#       TCP  192.168.2.135:49671  192.168.2.138:8081  TIME_WAIT
#       TCP  192.168.2.135:49673  192.168.2.138:8081  TIME_WAIT
#       ... 二十多个
#   后果:
#     1) 耗尽本机临时端口 (Windows 默认 16384, 也会被其它程序抢)
#     2) 每次新建连接都要走三次握手, 徒增延迟
#     3) 板子侧每个新连接都要分配 socket, 长连接一多就互相挤
#
#   http.client.HTTPConnection 支持 keep-alive, 响应读完不 close 就能
#   复用同一条 TCP 连接。下面用线程本地池实现。
import http.client
import threading


class _ConnPool:
    """按 (host, port) 复用 keep-alive 连接。

    【为什么是全局共享而不是线程本地】
    控制台是 WSGI 多线程模型, 每个请求一个线程。如果连接池是线程本地的,
    那么 N 个并发请求就会开 N 条 TCP 连接 —— 反而比每次新建更糟。
    共享池 + 一把锁, 保证所有请求复用同一条连接。

    实测: 板子的 IDF httpd 支持 HTTP/1.1 keep-alive (响应无
    Connection: close), 同一条连接可以连续请求很多次。
    """

    def __init__(self):
        self._conns = {}
        self._lock = threading.RLock()

    def _get_locked(self, host, port, timeout):
        key = (host, port)
        conn = self._conns.get(key)
        if conn is not None:
            try:
                if conn.sock is None:
                    conn = None
            except Exception:
                conn = None
        if conn is None:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
            self._conns[key] = conn
        return conn

    def get_locked(self, host, port, timeout):
        """调用方必须已持有 self.lock。"""
        return self._get_locked(host, port, timeout)

    def drop_locked(self, host, port):
        """调用方必须已持有 self.lock。"""
        conn = self._conns.pop((host, port), None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def get(self, host, port, timeout):
        with self._lock:
            return self._get_locked(host, port, timeout)

    def drop(self, host, port):
        with self._lock:
            self.drop_locked(host, port)

    @property
    def lock(self):
        return self._lock


POOL = _ConnPool()


def _request(device, path, method="GET", body=None, timeout=10, port=None):
    """走 keep-alive 连接请求板子。失败自动重连一次。

    整条请求在池锁内执行 —— 连接是共享的, 两个线程同时往同一条
    TCP 连接写数据会交错, HTTP 响应就会串。
    """
    port = port or DEVICE_PORT
    with POOL.lock:
        for attempt in (0, 1):
            conn = POOL.get_locked(device, port, timeout)
            try:
                headers = {}
                if body is not None:
                    headers["Content-Type"] = "application/octet-stream"
                    headers["Content-Length"] = str(len(body))
                conn.request(method, path, body=body, headers=headers)
                resp = conn.getresponse()
                data = resp.read()
                # 响应头里带 Connection: close 时必须丢弃这条连接
                if resp.getheader("Connection", "").lower() == "close":
                    POOL.drop_locked(device, port)
                return data
            except Exception:
                POOL.drop_locked(device, port)
                if attempt == 1:
                    raise
    raise RuntimeError("unreachable")


def fetch_bytes(device, path, timeout=10, port=None):
    return _request(device, path, "GET", None, timeout, port)


def fetch_json(device, path, timeout=8, port=None):
    raw = fetch_bytes(device, path, timeout, port)
    return json.loads(raw.decode("utf-8", "replace"))


def fetch_control(device, path, timeout=8):
    """控制面请求 —— 走 8081。

    固件做了数据面/控制面分离: MJPEG 把 8080 的 socket 槽位占满时,
    8081 依然毫秒级响应 (实测 15/15 成功, 69-303ms)。
    """
    return fetch_json(device, path, timeout, CONTROL_PORT)


class Board:
    """板子访问封装，带状态缓存。"""

    def __init__(self, device):
        self.device = device
        self.health = {}
        self.activity = {}
        self.lock = threading.Lock()

    def refresh(self):
        with self.lock:
            try:
                self.health = fetch_control(self.device, "/health")
            except Exception:
                self.health = {}
            try:
                self.activity = fetch_control(self.device, "/activity")
            except Exception:
                self.activity = {}

    def event(self, since=None):
        path = "/event"
        if since not in (None, ""):
            path += f"?since={since}"
        try:
            return fetch_control(self.device, path, timeout=4)
        except Exception:
            return {"type": "none"}


BOARD: Board = None


class SharedStream:
    """到板子 /stream 的【单条共享上游连接】。

    为什么必须共享: 板子的 http_server socket 槽位被 IDF 硬限制在 7
    (内部占 3, 客户端最多 4)。/stream 是长连接, 每个浏览器标签页各开一条
    上游连接的话, 几个标签页 + 代理重连就能把槽位占光 ->
    /health 等短请求全部排队 -> 板子"看着像挂了"。
    这里只维持一条上游, 多个客户端各自从缓冲区取数据, 互不干扰。

    ⚠️ 锁的用法要特别小心: threading.Condition(L) 内部用的就是 L,
       所以【不能】在持有 L 的同时再 `with self.cond:` —— 那是同一把
       不可重入锁, 会直接死锁 (第一版就是这么写的, 表现为所有流请求超时)。
    """

    def __init__(self, device):
        self.device = device
        self.cond = threading.Condition()
        self.buf = bytearray()      # 环形缓冲里的当前数据
        self.base = 0               # buf[0] 对应的绝对字节序号
        self.alive = False
        self.thread = None
        self.clients = 0            # 当前正在看画面的客户端数
        self.last_active = time.time()
        self.last_rx = time.time()  # 最后一次收到上游数据的时间

    def acquire(self):
        """有客户端接入。返回上游是否可用。"""
        with self.cond:
            self.clients += 1
            self.last_active = time.time()
        return self.ensure()

    def release(self):
        """客户端离开。"""
        with self.cond:
            self.clients = max(0, self.clients - 1)
            self.cond.notify_all()

    def stop_upstream(self):
        with self.cond:
            self.alive = False
            self.cond.notify_all()
        # _run 线程会看到 alive=False 并退出循环, 随后关闭 socket

    def _watchdog(self):
        """上游空闲看门狗。

        板子 http_server 的客户端 socket 槽位被 IDF 硬限制在 4 个。
        上游 /stream 长连接会一直占着 1 个。客户端全都断开后如果还占着,
        /health /activity /event 这些短请求就会排队甚至被拒。

        判据: 距离【最后一次收到上游数据】超过 8 秒 -> 判定上游已死, 断掉。
        板子正常出图时 /stream 每秒有几十 KB, 8 秒静默一定是出问题了。
        """
        while True:
            time.sleep(3)
            with self.cond:
                if not self.alive:
                    continue
                silent = time.time() - self.last_rx
                if silent > 8:
                    self.alive = False
                    self.cond.notify_all()

    def ensure(self):
        with self.cond:
            running = self.alive and self.thread and self.thread.is_alive()
            if not running:
                self.alive = True
                self.last_rx = 0        # 新连接: 先把"新鲜度"清零
                self.thread = threading.Thread(target=self._run, daemon=True)
                self.thread.start()
            # 等【新鲜】数据到达 (last_rx 在 8 秒内), 不能只看 buf 非空 ——
            # buf 里可能还留着上一次连接的陈旧数据。
            deadline = time.time() + 8
            while (time.time() - self.last_rx) > 3 and time.time() < deadline:
                self.cond.wait(0.2)
            if (time.time() - self.last_rx) > 3:
                raise RuntimeError("no fresh frame from device")
        return self

    def _run(self):
        import urllib.request
        while True:
            src = None
            try:
                src = urllib.request.urlopen(
                    f"http://{self.device}:{DEVICE_PORT}/stream", timeout=15)
                with self.cond:
                    if not self.alive:
                        # 没人看了, 刚连上就撤
                        try:
                            src.close()
                        except Exception:
                            pass
                        return
                    self.alive = True
                while True:
                    with self.cond:
                        if not self.alive:
                            break
                    chunk = src.read(4096)
                    if not chunk:
                        break
                    with self.cond:
                        self.buf.extend(chunk)
                        self.last_rx = time.time()
                        # 只保留最近 1MB, 防止慢客户端拖垮内存
                        if len(self.buf) > 1024 * 1024:
                            drop = len(self.buf) - 512 * 1024
                            del self.buf[:drop]
                            self.base += drop
                        self.cond.notify_all()
            except Exception:
                pass
            finally:
                if src is not None:
                    try:
                        src.close()
                    except Exception:
                        pass
            with self.cond:
                was_alive = self.alive
                self.alive = False
                self.cond.notify_all()
            if not was_alive:
                # 是主动停的, 不再重连
                return
            # 退避后重连, 避免疯狂重连把板子槽位打爆。
            # 但如果当前一个客户端都没有, 就不必再连了 —— 等下一个客户端
            # 调用 acquire() 时再重连。这样板子的 socket 槽位能彻底空出来。
            with self.cond:
                need = self.clients > 0
            if not need:
                return
            time.sleep(3)

    def reader(self):
        """生成器: 不断吐出新到达的字节。每个客户端独立游标。"""
        pos = self.base
        while True:
            with self.cond:
                while pos >= self.base + len(self.buf):
                    if not self.alive:
                        return
                    self.cond.wait(1.0)
                # 缓冲区被裁剪过, 游标落后太多就跳到当前起点
                if pos < self.base:
                    pos = self.base
                chunk = bytes(self.buf[pos - self.base:])
                pos = self.base + len(self.buf)
            yield chunk


STREAM = None

# --------------------------------------------------------------------------
# AI 对话桥（可选功能，惰性加载）
# --------------------------------------------------------------------------
# 故意不在模块顶层 import：控制台本身零第三方依赖，对话桥也是纯标准库实现，
# 配置没填或模块有问题时不能拖累原有的画面 / 对讲 / 状态功能。
CHAT = None


def get_chat(device):
    global CHAT
    if CHAT is None:
        import sys
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from chat_bridge import ChatBridge
        CHAT = ChatBridge(device=device)
    return CHAT


MODE = None


def get_mode(device):
    """模式切换器同样惰性加载：镜像或工具没配好也不能拖累原有页面。"""
    global MODE
    if MODE is None:
        import sys
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from mode_switch import ModeSwitcher
        MODE = ModeSwitcher(device)
    return MODE


def alaw_to_wav(alaw_bytes, sample_rate=ALAW_SAMPLE_RATE):
    """G.711 A-law -> 16-bit PCM WAV。

    Python 3.13 移除了 audioop，这里改用 chat_bridge 内置的查表解码
    （与 firmware/main/g711.c 一致），顺手把「录一段老人语音」救回来。
    """
    if audioop is not None:
        pcm = audioop.alaw2lin(alaw_bytes, 2)
    else:
        try:
            from chat_bridge import alaw_to_pcm
            pcm = alaw_to_pcm(alaw_bytes)
        except Exception:
            return None, None
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue(), pcm


# --------------------------------------------------------------------------
# 对讲采集 (PC 麦克风 -> PCM16 -> POST /audio/down)
# --------------------------------------------------------------------------
def collect_mic_pcm(duration_ms=3000):
    """用 PyAudio/或 wave 模块采集麦克风。优先 sounddevice，回退到 winsound。"""
    try:
        import sounddevice as sd
        import numpy as np
        sr = PCM_SAMPLE_RATE
        chunks = []
        with sd.InputStream(samplerate=sr, channels=1, dtype="int16",
                            blocksize=1024) as s:
            for _ in range(int(sr * duration_ms / 1000 / 1024) + 1):
                data, _ = s.read(1024)
                chunks.append(data.copy())
        pcm = np.concatenate(chunks).tobytes()
        return pcm
    except Exception as e:
        print(f"[warn] sounddevice 不可用 ({e})，尝试 wave 回退")
        return None


def post_pcm(device, pcm):
    """对讲下行 -> 板子喇叭。走控制面端口, 与画面互不干扰。"""
    return _request(device, "/audio/down", "POST", pcm, 20, CONTROL_PORT)


# --------------------------------------------------------------------------
# WSGI 应用
# --------------------------------------------------------------------------
def app(environ, start_response):
    path = environ.get("PATH_INFO", "/")
    method = environ.get("REQUEST_METHOD", "GET")
    device = BOARD.device

    def json_resp(obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        start_response(f"{code} OK" if code == 200 else f"{code} Error",
                       [("Content-Type", "application/json; charset=utf-8"),
                        ("Content-Length", str(len(body)))])
        return [body]

    # ---- 页面 ----
    if path == "/":
        body = INDEX_HTML.encode("utf-8")
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                                  ("Content-Length", str(len(body)))])
        return [body]

    # ---- MJPEG 代理 ----
    if path == "/view/stream":
        try:
            up = STREAM.acquire()
        except Exception as e:
            body = f"device stream unavailable: {e}".encode("utf-8")
            start_response("502 Bad Gateway",
                           [("Content-Type", "text/plain; charset=utf-8"),
                            ("Content-Length", str(len(body)))])
            return [body]

        def gen():
            try:
                yield from up.reader()
            finally:
                up.release()

        start_response("200 OK",
                       [("Content-Type", "multipart/x-mixed-replace; boundary=frame"),
                        ("Cache-Control", "no-store")])
        return gen()

    # ---- API 转发 ----
    if path == "/api/health":
        try:
            return json_resp(fetch_control(device, "/health"))
        except Exception as e:
            return json_resp({"error": str(e)}, 502)

    if path == "/api/activity":
        try:
            return json_resp(fetch_control(device, "/activity"))
        except Exception as e:
            return json_resp({"error": str(e)}, 502)

    if path == "/api/event":
        since = environ.get("QUERY_STRING", "")
        since = since.split("since=")[1] if "since=" in since else None
        try:
            return json_resp(BOARD.event(since))
        except Exception as e:
            return json_resp({"type": "none", "error": str(e)}, 502)

    if path == "/api/notify" and method == "POST":
        try:
            n = int(environ.get("CONTENT_LENGTH") or 0)
            body = environ["wsgi.input"].read(n) if n else b""
            # 固件的 /notify 是 POST, 必须把 body 转发过去
            _request(device, "/notify", "POST", body or b"", 5, CONTROL_PORT)
            return json_resp({"ok": True})
        except Exception as e:
            return json_resp({"ok": False, "error": str(e)}, 502)

    # ---- 老人语音上行: 录 3 秒 -> WAV ----
    if path == "/api/record":
        try:
            import urllib.request
            # 走控制面端口: 画面占满数据面时也能录到老人语音
            src = urllib.request.urlopen(
                f"http://{device}:{CONTROL_PORT}/audio/alaw", timeout=20)
            alaw = b""
            deadline = time.time() + 3.0
            while time.time() < deadline:
                chunk = src.read(4096)
                if not chunk:
                    break
                alaw += chunk
                if len(alaw) > 200000:
                    break
            src.close()
            wav, _ = alaw_to_wav(alaw)
            if wav is None:
                return json_resp({"error": "A-law 解码不可用 (chat_bridge 加载失败)"}, 500)
            start_response("200 OK", [("Content-Type", "audio/wav"),
                                      ("Content-Length", str(len(wav)))])
            return [wav]
        except Exception as e:
            return json_resp({"error": str(e)}, 500)

    # ---- 对讲下行: 浏览器麦克风 (PCM16 16k) -> 板子喇叭 ----
    if path == "/api/talk" and method == "POST":
        try:
            n = int(environ.get("CONTENT_LENGTH") or 0)
            pcm = environ["wsgi.input"].read(n) if n else b""
            if not pcm:
                return json_resp({"ok": False, "error": "empty body"}, 400)
            post_pcm(device, pcm)
            return json_resp({"ok": True, "bytes": len(pcm)})
        except Exception as e:
            return json_resp({"ok": False, "error": str(e)}, 500)

    # ---- AI 对话桥: 开关 / 状态 / 文字调试 ----
    if path.startswith("/api/chat/"):
        try:
            chat = get_chat(device)
        except Exception as e:
            return json_resp({"running": False, "state": "error", "detail": "-",
                              "error": "对话桥加载失败: %s" % e,
                              "backend": "-", "config_problems": [],
                              "rounds": [], "stats": {}}, 500)
        if path == "/api/chat/status":
            return json_resp(chat.status())
        if method == "POST":
            n = int(environ.get("CONTENT_LENGTH") or 0)
            body = environ["wsgi.input"].read(n).decode("utf-8", "replace") if n else ""
            if path == "/api/chat/start":
                return json_resp(chat.start())
            if path == "/api/chat/stop":
                return json_resp(chat.stop())
            if path == "/api/chat/say":
                return json_resp(chat.say(body))
        return json_resp({"ok": False, "msg": "unknown chat api"}, 404)

    # ---- ⑤ 模式切换: 看护固件 <-> 小智固件 (USB 串口整片烧录) ----
    if path.startswith("/api/mode/"):
        try:
            ms = get_mode(device)
        except Exception as e:
            return json_resp({"ready": False, "mode": "unknown", "busy": False,
                              "problems": ["模式切换模块加载失败: %s" % e],
                              "log": [], "last_result": ""}, 500)
        if path == "/api/mode/status":
            return json_resp(ms.status())
        if path == "/api/mode/switch" and method == "POST":
            return json_resp(ms.switch())
        return json_resp({"ok": False, "msg": "unknown mode api"}, 404)

    start_response("404 Not Found", [("Content-Type", "text/plain")])
    return [b"not found"]


def webm_to_pcm(webm_bytes):
    """浏览器 MediaRecorder 输出 webm/opus。解成 PCM16 16k 单声道。

    依赖 ffmpeg；没有 ffmpeg 时返回 None。
    （当前前端已改用 AudioContext 直接抓 PCM，这条路径只作备用。）
    """
    import subprocess, tempfile
    if not webm_bytes:
        return None
    ffmpeg = shutil_which("ffmpeg")
    if not ffmpeg:
        return None
    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as f:
        f.write(webm_bytes)
        src = f.name
    dst = src + ".pcm"
    try:
        subprocess.run([ffmpeg, "-y", "-i", src,
                        "-f", "s16le", "-acodec", "pcm_s16le",
                        "-ar", str(PCM_SAMPLE_RATE), "-ac", "1", dst],
                       check=True, capture_output=True, timeout=20)
        with open(dst, "rb") as f:
            return f.read()
    except Exception:
        return None
    finally:
        for p in (src, dst):
            try:
                os.unlink(p)
            except OSError:
                pass


def shutil_which(name):
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def main():
    global BOARD, STREAM
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default=DEFAULT_DEVICE)
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    BOARD = Board(args.device)
    global STREAM
    STREAM = SharedStream(args.device)
    threading.Thread(target=STREAM._watchdog, daemon=True).start()

    def bg():
        while True:
            BOARD.refresh()
            time.sleep(2)

    threading.Thread(target=bg, daemon=True).start()

    print(f"设备: {args.device}:{DEVICE_PORT}")
    print(f"控制台: http://{args.host}:{args.port}")
    print("AI 对话: 页面「④ AI 对话」里开启；配置见 pc_receiver/chat_config.json")
    print("模式切换: 页面「⑤ 模式切换」一键在看护固件 ⇄ 小智 AI 之间切换（USB 串口烧录）")
    print("Ctrl+C 退出")

    # 必须多线程: MJPEG 是长连接, 单线程 wsgiref 会把 /api/talk 等请求全堵住
    from socketserver import ThreadingMixIn
    from wsgiref.simple_server import WSGIServer

    class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
        daemon_threads = True
        allow_reuse_address = True

    httpd = make_server(args.host, args.port, app,
                        server_class=ThreadingWSGIServer)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
