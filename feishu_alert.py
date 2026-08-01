"""
飞书每日定投提醒脚本
监控:
  - 中证红利 (sh000922) < MA120 → 买入信号
  - 红利低波 (H30269)   < MA120 → 买入信号
  - 中证500  (sh000905) < MA250 → 买入信号
每天早上 9:00 (开盘前) 推送飞书机器人

===== 设置 =====
1. 在飞书群添加"自定义机器人", 复制 webhook URL (含 token)
2. 设置环境变量: export FEISHU_WEBHOOK_URL="https://open.feishu.cn/open-apis/bot/v2/hook/xxxxx"
3. 设置 cron:
   crontab -e
   # 每天 9:00 跑
   0 9 * * 1-5 cd /path/to/project && /path/to/python feishu_alert.py >> feishu_alert.log 2>&1

测试:
  python feishu_alert.py --dry-run   # 只打印不发
"""
import os
import sys
import json
import argparse
import requests
import akshare as ak
import pandas as pd
from datetime import datetime

# === 配置 ===
FEISHU_WEBHOOK_URL = os.environ.get(
    'FEISHU_WEBHOOK_URL',
    ''  # 留空: 必须用环境变量; 或直接填 "https://open.feishu.cn/open-apis/bot/v2/hook/xxxxx"
)

# 监控列表
WATCH_LIST = [
    {'name': '中证红利', 'code': 'sh000922', 'ma': 120, 'source': 'csi',     'emoji': '🟢'},
    {'name': '红利低波', 'code': 'H30269',   'ma': 120, 'source': 'csindex', 'emoji': '🔵'},
    {'name': '中证500',  'code': 'sh000905', 'ma': 250, 'source': 'csi',     'emoji': '🟡'},
]


def fetch_data(code, source, lookback_days=400):
    """拉取指数数据"""
    if source == 'csi':
        df = ak.stock_zh_index_daily(symbol=code)
        df.columns = [c.lower() for c in df.columns]
        df['date'] = pd.to_datetime(df['date'])
    else:  # csindex
        end = datetime.now().strftime('%Y%m%d')
        df = ak.stock_zh_index_hist_csindex(symbol=code, start_date='20100101', end_date=end)
        df = df.rename(columns={'日期': 'date', '收盘': 'close'})
        df['date'] = pd.to_datetime(df['date'])
    df = df[['date', 'close']].dropna().drop_duplicates('date').set_index('date').sort_index()
    return df.tail(lookback_days)


def get_signal(df, ma_window):
    """计算 MA 并生成 buy/hold 信号"""
    if len(df) < ma_window:
        return None
    ma = df['close'].rolling(ma_window).mean()
    current_price = float(df['close'].iloc[-1])
    current_ma = float(ma.iloc[-1])
    pct_diff = (current_price / current_ma - 1) * 100
    return {
        'price': current_price,
        'ma_value': current_ma,
        'pct_diff': pct_diff,
        'signal': 'buy' if current_price < current_ma else 'hold',
        'date': df.index[-1].strftime('%Y-%m-%d'),
        'prev_close': float(df['close'].iloc[-2]) if len(df) >= 2 else current_price,
    }


def build_card(results):
    """构建飞书 interactive card"""
    buy_items = [r for r in results if r['signal'] == 'buy']
    hold_items = [r for r in results if r['signal'] == 'hold']

    if buy_items:
        title = f"🔔 今日有 {len(buy_items)} 个标的在 MA 下方, 可考虑买入"
        template = "red"
    else:
        title = "✅ 今日全部在 MA 上方, 无买入信号"
        template = "green"

    elements = []

    # 标的明细
    md_lines = []
    for r in results:
        emoji = "🟢" if r['signal'] == 'buy' else "⚪"
        action = "**建议买入**" if r['signal'] == 'buy' else "继续持有 / 不买"
        md_lines.append(
            f"{emoji} **{r['name']}** ({r['ma']}日均线)\n"
            f"   现价: **{r['price']:.2f}**  |  MA{r['ma']}: {r['ma_value']:.2f}  |  偏离: **{r['pct_diff']:+.2f}%**\n"
            f"   状态: {action}  |  数据日期: {r['date']}"
        )
    elements.append({
        "tag": "div",
        "text": {"tag": "lark_md", "content": "\n\n".join(md_lines)}
    })

    # 分隔
    elements.append({"tag": "hr"})

    # 操作建议
    if buy_items:
        items = ", ".join([f"**{r['name']}**" for r in buy_items])
        suggestion = f"### 🎯 操作建议\n\n• 买入以下: {items}\n• 建议分批, 不要一次性满仓"
    else:
        suggestion = "### 🎯 操作建议\n\n• 今日无操作, 继续等待 MA 跌破信号"
    elements.append({
        "tag": "div",
        "text": {"tag": "lark_md", "content": suggestion}
    })

    # 备注
    elements.append({
        "tag": "note",
        "elements": [{
            "tag": "plain_text",
            "content": f"查询时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  |  数据源: akshare"
        }]
    })

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
            "elements": elements,
        }
    }


def send_to_feishu(webhook_url, card):
    """发送 interactive card 到飞书"""
    if not webhook_url:
        return None
    headers = {'Content-Type': 'application/json; charset=utf-8'}
    try:
        r = requests.post(webhook_url, headers=headers, data=json.dumps(card), timeout=15)
        return r.json()
    except Exception as e:
        return {'error': str(e)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true', help='只打印不发飞书')
    args = parser.parse_args()

    print(f"[{datetime.now()}] 开始检查 MA 跌破信号...")

    results = []
    for item in WATCH_LIST:
        try:
            df = fetch_data(item['code'], item['source'])
            sig = get_signal(df, item['ma'])
            if sig:
                sig.update({'name': item['name'], 'ma': item['ma']})
                results.append(sig)
                emoji = "🟢 BUY" if sig['signal'] == 'buy' else "⚪ HOLD"
                print(f"  {item['name']:8s} MA{item['ma']:3d}: 现价 {sig['price']:.2f}  MA {sig['ma_value']:.2f}  偏离 {sig['pct_diff']:+.2f}%  → {emoji}")
        except Exception as e:
            print(f"  {item['name']} 失败: {e}")

    if not results:
        print("无数据, 退出")
        return

    # 构建飞书消息
    card = build_card(results)

    if args.dry_run:
        print("\n[dry-run] 飞书消息内容:")
        print(json.dumps(card, ensure_ascii=False, indent=2))
        return

    if not FEISHU_WEBHOOK_URL:
        print("\n未设置 FEISHU_WEBHOOK_URL 环境变量, 跳过发送")
        print("设置方法: export FEISHU_WEBHOOK_URL='https://open.feishu.cn/open-apis/bot/v2/hook/xxxxx'")
        return

    result = send_to_feishu(FEISHU_WEBHOOK_URL, card)
    if result and result.get('StatusCode') == 0 or (result and result.get('code') == 0):
        print(f"  ✓ 飞书消息已发送")
    else:
        print(f"  ✗ 飞书发送失败: {result}")


if __name__ == '__main__':
    main()
