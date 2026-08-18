"""显示本机已启动的 MetaTrader 5 终端中的当前持仓。

安装一次依赖：py -m pip install MetaTrader5
运行：py view_mt5_positions.py
"""

from datetime import datetime, timezone

import MetaTrader5 as mt5


def main() -> None:
    if not mt5.initialize():
        raise SystemExit(f"无法连接 MT5：{mt5.last_error()}\n请先启动并登录 MT5 终端。")

    try:
        account = mt5.account_info()
        positions = mt5.positions_get()
        if account is None or positions is None:
            raise SystemExit(f"读取账户/持仓失败：{mt5.last_error()}")

        print(f"账户: {account.login} | 余额: {account.balance:.2f} | 净值: {account.equity:.2f}")
        print(f"持仓数: {len(positions)}")
        if not positions:
            return

        print("-" * 110)
        print(f"{'Ticket':<12} {'品种':<14} {'方向':<6} {'手数':>8} {'开仓价':>14} {'现价':>14} {'浮盈':>14} {'开仓时间'}")
        for p in positions:
            side = "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL"
            opened = datetime.fromtimestamp(p.time, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
            print(
                f"{p.ticket:<12} {p.symbol:<14} {side:<6} {p.volume:>8.2f} "
                f"{p.price_open:>14.5f} {p.price_current:>14.5f} {p.profit:>14.2f} {opened}"
            )
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
