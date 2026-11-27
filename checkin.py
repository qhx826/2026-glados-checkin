#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
2026 GLaDOS 自动签到 (积分增强版)

功能：
- 全自动签到（新版 challenge/proof 协议，2026-11-07 官方调整后必需）
- 精准获取当前积分 (Points)
- PushPlus 微信推送（包含积分、剩余天数、签到结果）
- 智能多域名切换 (优先 glados.cloud)
- 支持 Cookie-Editor 导出格式

新协议流程：
  1) GET /api/user/checkin/challenge -> { code:0, nonce, ttl }
  2) 等待 >= 2500ms（前端强制的人类反应时间下限，<=5s 为 excellent 档）
  3) POST /api/user/checkin body: { token:<hostname>, nonce, proof }
     proof = SHA256_HEX(nonce + "." + "glados-observe-v1")
  缺 nonce/proof 的裸请求服务端仍返回 code:0，但只给 Base 档保底积分。
"""

import requests
import json
import os
import sys
import time
import re
import random
import hashlib
from datetime import datetime

# Fix Windows Unicode Output
if sys.platform.startswith('win'):
    sys.stdout.reconfigure(encoding='utf-8')

# ================= 配置 =================

# 域名优先级：Cloud 第一
DOMAINS = [
    "https://glados.cloud",
    "https://glados.rocks", 
    "https://glados.network",
]

# UA：可用 GLADOS_UA 环境变量覆盖为登录浏览器的真实 UA
# （服务端会做设备校验，UA 与登录设备不一致会返回 code=4 device-mismatch）
UA = os.environ.get('GLADOS_UA', '').strip() or 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/158.0.0.0 Safari/537.36'

def _chrome_major(ua):
    m = re.search(r'Chrome/(\d+)', ua or '')
    return m.group(1) if m else '141'

_CHROME_MAJOR = _chrome_major(UA)

HEADERS = {
    'User-Agent': UA,
    'Content-Type': 'application/json;charset=UTF-8',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'sec-ch-ua': f'"Chromium";v="{_CHROME_MAJOR}", "Not=A?Brand";v="24", "Google Chrome";v="{_CHROME_MAJOR}"',
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-platform': '"Windows"',
    'sec-fetch-dest': 'empty',
    'sec-fetch-mode': 'cors',
    'sec-fetch-site': 'same-origin',
}

PROOF_SALT = 'glados-observe-v1'

# ================= 工具函数 =================

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}")

def extract_cookie(raw: str):
    """提取 Cookie，支持 Cookie-Editor 冒号格式"""
    if not raw: return None
    raw = raw.strip()
    
    # Cookie-Editor 格式 (koa:sess=xxx; koa:sess.sig=yyy)
    if 'koa:sess=' in raw or 'koa:sess.sig=' in raw:
        return raw
        
    # JSON
    if raw.startswith('{'):
        try:
            return 'koa.sess=' + json.loads(raw).get('token')
        except: pass
        
    # JWT Token
    if raw.count('.') == 2 and '=' not in raw and len(raw) > 50:
        return 'koa:sess=' + raw
        
    # Standard (gld:sess=...; gld:sess.sig=...; gld:accts=...; gld:accts.sig=...)
    return raw

def get_cookies():
    raw = os.environ.get("GLADOS_COOKIE", "")
    if not raw:
        log("❌ 未配置 GLADOS_COOKIE")
        return []
    
    # Split by enter or &
    sep = '\n' if '\n' in raw else '&'
    return [extract_cookie(c) for c in raw.split(sep) if c.strip()]

# ================= 核心逻辑 =================

class GLaDOS:
    def __init__(self, cookie):
        self.cookie = cookie
        self.domain = DOMAINS[0]
        self.email = "?"
        self.left_days = "?"
        self.points = "?"
        self.points_change = "?"
        self.exchange_info = ""
        self.plan = "?"
        
    def req_domain(self, domain, method, path, data=None):
        """向指定域名发一次请求"""
        try:
            url = f"{domain}{path}"
            h = HEADERS.copy()
            h['Cookie'] = self.cookie
            h['Origin'] = domain
            h['Referer'] = f"{domain}/console/checkin"
            
            if method == 'GET':
                resp = requests.get(url, headers=h, timeout=10)
            else:
                resp = requests.post(url, headers=h, json=data, timeout=10)
            
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            log(f"⚠️ {domain} 请求失败: {e}")
        return None

    def req(self, method, path, data=None):
        """带自动域名切换的请求"""
        for d in DOMAINS:
            res = self.req_domain(d, method, path, data)
            if res is not None:
                self.domain = d # Remember working domain
                return res
        return None

    def get_status(self):
        """获取状态：天数、邮箱"""
        res = self.req('GET', '/api/user/status')
        if res and 'data' in res:
            d = res['data']
            self.email = d.get('email', 'Unknown')
            self.left_days = str(d.get('leftDays', '?')).split('.')[0]
            return True
        return False

    def get_points(self):
        """获取积分、变化历史、兑换计划"""
        res = self.req('GET', '/api/user/points')
        if res and 'points' in res:
            # 当前积分
            self.points = str(res.get('points', '0')).split('.')[0]
            
            # 最近一次积分变化
            history = res.get('history', [])
            if history:
                last = history[0]
                change = str(last.get('change', '0')).split('.')[0]
                if not change.startswith('-'):
                    change = '+' + change
                self.points_change = change
            
            # 兑换计划
            plans = res.get('plans', {})
            pts = int(self.points)
            exchange_lines = []
            for plan_id, plan_data in plans.items():
                need = plan_data['points']
                days = plan_data['days']
                if pts >= need:
                    exchange_lines.append(f"✅ {need}分→{days}天 (可兑换)")
                else:
                    exchange_lines.append(f"❌ {need}分→{days}天 (差{need-pts}分)")
            self.exchange_info = "<br>".join(exchange_lines)
            return True
        return False

    def checkin(self):
        """执行签到（新版 challenge/proof 协议）"""
        # 1. 取挑战
        ch = self.req('GET', '/api/user/checkin/challenge')
        if ch and ch.get('code') == 0 and ch.get('nonce'):
            nonce = str(ch['nonce'])
            domain = self.domain  # 挑战成功的域名，签到必须同域
            host = domain.replace('https://', '').replace('http://', '')
            # 2. 等待人类反应时间（前端下限 2500ms，excellent 档 <=5s）
            wait = 2.8 + random.random() * 1.7
            log(f"挑战已获取，等待 {wait:.1f}s 后提交")
            time.sleep(wait)
            # 3. 计算 proof 并提交
            proof = hashlib.sha256(f"{nonce}.{PROOF_SALT}".encode('utf-8')).hexdigest()
            return self.req_domain(domain, 'POST', '/api/user/checkin', {'token': host, 'nonce': nonce, 'proof': proof})
        log("⚠️ 挑战接口未返回 nonce，改用旧协议裸签（只能拿保底分）")
        return self.req('POST', '/api/user/checkin', {'token': 'glados.cloud'})

# ================= 主程序 =================

def pushplus(token, title, content):
    if not token: return
    try:
        url = "http://www.pushplus.plus/send"
        requests.get(url, params={'token': token, 'title': title, 'content': content, 'template': 'html'}, timeout=5)
        log("✅ PushPlus 推送成功")
    except:
        log("❌ PushPlus 推送失败")

def main():
    log("🚀 2026 GLaDOS Checkin Starting...")
    cookies = get_cookies()
    if not cookies: sys.exit(1)
    
    results = []
    success_cnt = 0
    
    for i, cookie in enumerate(cookies, 1):
        g = GLaDOS(cookie)
        
        # 1. Checkin
        res = g.checkin()
        msg = res.get('message', 'Failure') if res else "Network Error"
        if res and res.get('code') == 4:
            log("❌ 设备校验不通过（device-mismatch）：脚本 UA 与登录浏览器不一致，需把 GLADOS_UA 换成登录浏览器的 UA")
        
        # 2. Get Info (Refresh data)
        g.get_status()
        g.get_points()
        
        # 3. Log
        status_icon = "✅" if "Checkin" in msg else "⚠️"
        log(f"用户: {g.email} | 积分: {g.points} | 天数: {g.left_days} | 结果: {msg}")
        
        if "Checkin" in msg: success_cnt += 1
        
        # 4. Result Formatting
        results.append(f"""
<div style="border:2px solid #333; padding:15px; margin-bottom:15px; border-radius:10px; background:#fff;">
    <h3 style="margin:0 0 15px 0; color:#333; border-bottom:2px solid #333; padding-bottom:8px;">👤 {g.email}</h3>
    <p style="margin:8px 0; color:#000; font-size:16px;"><b>当前积分:</b> <span style="color:#e74c3c; font-size:22px; font-weight:bold;">{g.points}</span> <span style="color:#27ae60; font-weight:bold;">({g.points_change})</span></p>
    <p style="margin:8px 0; color:#000; font-size:16px;"><b>剩余天数:</b> <span style="font-weight:bold;">{g.left_days} 天</span></p>
    <p style="margin:8px 0; color:#000; font-size:16px;"><b>签到结果:</b> {msg}</p>
    <div style="margin-top:15px; padding:12px; background:#f0f0f0; border-radius:8px; border:1px solid #ccc;">
        <p style="margin:0 0 8px 0; color:#333; font-weight:bold; font-size:15px;">🎁 兑换选项:</p>
        <p style="margin:0; color:#000; font-size:14px; line-height:1.8;">{g.exchange_info}</p>
    </div>
</div>
""")

    # Push
    ptoken = os.environ.get("PUSHPLUS_TOKEN")
    if ptoken:
        # Get first user's points for title
        first_points = "多账户"
        if len(cookies) == 1:
            # Re-parse log to find points? Or just use last object
            # Ideally store objects. Using simplified approach:
            pass 
        
        title = f"GLaDOS签到: 成功{success_cnt}/{len(cookies)}"
        content = "".join(results)
        content += f"<br><small>时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</small>"
        pushplus(ptoken, title, content)

if __name__ == '__main__':
    main()
