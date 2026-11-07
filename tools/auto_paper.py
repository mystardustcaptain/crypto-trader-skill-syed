#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unattended PAPER simulation for the syed-v1 experiment.

    python3 tools/auto_paper.py

A scheduler can run that every 15 minutes. Each run scans the default setups,
keeps the triggers the entry rules still call fresh, sizes and risk-checks them
with this repo's own limits, and simulates the fill from the order book. Later
runs play stops, targets and the time limit on 1-minute bars that opened after
the fill and had already closed. One JSON line goes to stdout.

This file never places an order and never reads API keys. "paper": false, or
--live, makes it refuse. Anything that can reach an exchange stays on
crypto-broker, which still shows a ticket and still waits for a typed yes.
"""

import argparse
import contextlib
import fcntl
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "crypto-setup"))
import cryptocore as cc  # noqa: E402

M1 = 60000
DECISION_MINUTES_DEFAULT = 15
BAR_MS = {"orb": 300000, "breakout": 900000, "trend": 900000, "volume": 900000,
          "reversal": 3600000, "squeeze": 3600000}
# Risk-check reasons that count as a limit being hit. "reward:risk" carries its own colon.
LIMIT_NAMES = ("reward:risk", "daily loss", "trades", "positions", "cooldown", "costs", "loss",
               "liquidation", "margin", "leverage", "duplicate", "liquidity", "time", "moved",
               "stop", "target", "size", "short", "blocked", "format")
OVERLAY_KEYS = ("max_daily_loss_usd", "max_trades_per_day", "max_open_positions", "max_cost_r",
                "cooldown_after_losses", "cooldown_hours", "min_liq_buffer", "max_margin_pct",
                "min_reward_risk", "max_leverage", "margin_mode", "allow_cross_margin",
                "allow_short", "require_stop", "max_hold_hours", "risk_day_tz")
_MODS = {}


def load_mod(folder, name):
    key = (folder, name)
    if key not in _MODS:
        path = os.path.join(ROOT, folder, name + ".py")
        spec = importlib.util.spec_from_file_location("auto_paper_%s_%s" % (folder.replace("-", "_"), name), path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _MODS[key] = mod
    return _MODS[key]


class _SizeArgs(object):
    """What crypto-budget.size reads off the command line. Market orders, no extra flags."""

    def __init__(self):
        self.limit = None
        self.risk = None
        self.leverage = None


def paper_profile(cfg):
    """Shipped paper profile, overlaid with the user's paper_profile object. Never mutates defaults."""
    profile = dict(cc.DEFAULTS["paper_profile"])
    custom = cfg.get("paper_profile")
    if isinstance(custom, dict):
        profile.update(custom)
    return profile


def risk_budget_usd(equity, profile):
    """min(risk_pct of current equity, the paper cap). Zero when the experiment is wiped out."""
    pct = float(profile.get("risk_pct") if profile.get("risk_pct") is not None else 0.01)
    cap = float(profile.get("max_loss_per_trade_usd") if profile.get("max_loss_per_trade_usd") is not None else 10)
    if equity <= 0 or pct <= 0:
        return 0.0
    return min(equity * pct, cap)


def experiment_config(base, profile, equity):
    """Limits for this run only. The file on disk, and the live defaults, stay as they are."""
    cfg = dict(base)
    cfg["paper"] = True
    cfg["paper_profile"] = dict(profile)
    cfg["account_usd"] = equity
    cfg["max_loss_per_trade_usd"] = risk_budget_usd(equity, profile)
    for key in OVERLAY_KEYS:
        if key in profile and profile[key] is not None:
            cfg[key] = profile[key]
    cfg["risk_day_tz"] = profile.get("risk_day_tz") or "UTC"
    return cfg


def ledger_paths(profile):
    rules = str(profile.get("rules") or "syed-v1")
    slug = "".join(ch if (ch.isalnum() or ch in "-_") else "-" for ch in rules).strip("-") or "syed-v1"
    directory = cc.wpath("experiments", slug)
    return directory, os.path.join(directory, "ledger.jsonl")


def skip_reason(hit, now, decision_minutes):
    """None when this hit is still a fresh entry. Otherwise a short reason.

    The status filters are the entry timer's: an extended breakout is not a new
    trigger, a squeeze with no side is not a trade, relative strength has no stop.
    The clock window is the longer of the setup's own bar and one scheduler bar
    (15 minutes), so a run every 15 minutes still sees the opening-range break
    that closed a few minutes earlier, and still skips the one from an hour ago.
    """
    setup = hit.get("setup")
    side = hit.get("side")
    if setup == "rs":
        return "ranking"
    if side not in cc.SIDES:
        return "no_side"
    if hit.get("stop_ref") is None:
        return "no_stop"
    if setup == "breakout" and hit.get("status") != "fresh":
        return "stale"
    if setup == "trend" and hit.get("status") == "extended":
        return "stale"
    step = BAR_MS.get(setup)
    if step is None:
        return "not_an_entry"
    window = max(step, int(decision_minutes * 60000))
    if not hit.get("at") or hit["at"] <= now - window:
        return "stale"
    return None


def signal_id(hit):
    return "%s|%s|%s|%d" % (hit.get("coin"), hit.get("setup"), hit.get("side"), int(hit.get("at") or 0))


def limit_name(reason):
    for name in LIMIT_NAMES:
        if reason.startswith(name):
            return name
    head = reason.split(":", 1)[0].strip()
    return head or "risk"


def walk_qty(levels, qty):
    """Average price for `qty` coins against one side of the book. Same walk the broker uses."""
    left, cost, got, worst = qty, 0.0, 0.0, None
    for price, size in levels:
        take = min(size, left)
        cost += take * price
        got += take
        worst = price
        left -= take
        if left <= 1e-12:
            break
    return (cost / got if got else None), got, worst


def market_fill(venue, market, side, qty):
    """Simulated market fill against the public book. Reads only. Returns (price, note)."""
    book_side = "buy" if side == "long" else "sell"
    bids, asks = venue.orderbook(market["symbol"], 500 if venue.name == "binance" else 200)
    levels = asks if book_side == "buy" else bids
    if not levels:
        raise cc.CryptoError("the %s order book is empty right now" % market["symbol"])
    avg, got, worst = walk_qty(levels, qty)
    note = None
    if got < qty - 1e-12:
        pen = 1.002 if book_side == "buy" else 0.998
        rest = qty - got
        avg = ((avg or worst) * got + worst * pen * rest) / qty
        note = "the visible book was thin; part of the fill is estimated past its last level"
    if market.get("tick"):
        avg = round(avg, cc.tick_decimals(market["tick"]) + 2)
    return avg, note


def walk_position(pos, bars, fund, now):
    """Stop, target, liquidation, funding and the time limit.

    Bars that opened before the fill, or that have not closed by `now`, are ignored.
    A bar that touches the stop and the target is a stop. Returns
    (close or None, funding_paid). close is (price, exit_reason, at_ms, fee_out).
    """
    opened = pos["opened_at"]
    costs = pos.get("costs") or {}
    taker = costs.get("taker", 0.0005)
    maker = costs.get("maker", 0.0002)
    slip = costs.get("slippage")
    if slip is None:
        slip = cc.DEFAULT_SLIPPAGE
    events = sorted(fund or [])
    fi = 0
    sg = cc.side_sign(pos["side"])
    stop, target, liq = pos["stop"], pos["target"], pos.get("liq")
    funding_paid = 0.0
    qty = pos["qty"]
    for b in bars:
        if b[cc.T] < opened or b[cc.T] + M1 > now:
            continue
        while fi < len(events) and events[fi][0] <= b[cc.T]:
            if events[fi][0] > opened:
                funding_paid += sg * events[fi][1] * b[cc.O] * qty
            fi += 1
        if pos.get("exit_by") and b[cc.T] >= pos["exit_by"]:
            px = b[cc.O] * (1 - sg * slip)
            return (px, "time limit", b[cc.T], qty * px * taker), funding_paid
        if liq and pos["side"] == "long" and liq >= stop and b[cc.L] <= liq:
            return (liq, "liquidation", b[cc.T] + M1, pos.get("margin", 0) - qty * (pos["entry"] - liq)), funding_paid
        if liq and pos["side"] == "short" and liq <= stop and b[cc.H] >= liq:
            return (liq, "liquidation", b[cc.T] + M1, pos.get("margin", 0) - qty * (liq - pos["entry"])), funding_paid
        if pos["side"] == "long":
            if b[cc.O] <= stop:
                if liq and b[cc.O] <= liq:
                    return (liq, "liquidation", b[cc.T], pos.get("margin", 0) - qty * (pos["entry"] - liq)), funding_paid
                px = b[cc.O] * (1 - slip)
                return (px, "stop", b[cc.T], qty * px * taker), funding_paid
            if b[cc.L] <= stop:
                px = stop * (1 - slip)
                return (px, "stop", b[cc.T] + M1, qty * px * taker), funding_paid
            if target is not None and b[cc.H] >= target:
                return (target, "target", b[cc.T] + M1, qty * target * maker), funding_paid
        else:
            if b[cc.O] >= stop:
                if liq and b[cc.O] >= liq:
                    return (liq, "liquidation", b[cc.T], pos.get("margin", 0) - qty * (liq - pos["entry"])), funding_paid
                px = b[cc.O] * (1 + slip)
                return (px, "stop", b[cc.T], qty * px * taker), funding_paid
            if b[cc.H] >= stop:
                px = stop * (1 + slip)
                return (px, "stop", b[cc.T] + M1, qty * px * taker), funding_paid
            if target is not None and b[cc.L] <= target:
                return (target, "target", b[cc.T] + M1, qty * target * maker), funding_paid
    return None, funding_paid


def funding_events(venue, pos, now):
    start = pos["opened_at"]
    try:
        hist = venue.funding_history(pos["symbol"], start, now)
    except cc.CryptoError:
        hist = []
    if hist:
        return sorted(((t // M1) * M1, r) for t, r in hist)
    hours = pos.get("funding_hours") or 8.0
    return [(f, pos.get("funding_rate") or 0.0) for f in cc.funding_times(start, now, hours)]


def open_positions(events):
    """Opens that have no matching close, in ledger order. The ledger is the only source."""
    opened = {}
    order = []
    for event in events:
        if event.get("type") == "open" and event.get("id"):
            opened[event["id"]] = event
            order.append(event["id"])
        elif event.get("type") == "close" and event.get("id"):
            opened.pop(event["id"], None)
    return [opened[i] for i in order if i in opened]


def start_equity(events, profile):
    for event in events:
        if event.get("type") == "experiment" and event.get("start_equity_usd") is not None:
            return float(event["start_equity_usd"])
    return float(profile.get("start_equity_usd") if profile.get("start_equity_usd") is not None else 1000)


def realized_pnl(events):
    return sum(cc.fnum(e.get("pnl_usd"), 0.0) for e in events if e.get("type") == "close")


def day_pnl(events, start_ms, now):
    total = 0.0
    for event in events:
        if event.get("type") != "close":
            continue
        at = event.get("at", 0)
        if start_ms <= at <= now:
            total += cc.fnum(event.get("pnl_usd"), 0.0)
    return total


def unrealized(pos, last, funding_paid):
    gross = cc.side_sign(pos["side"]) * (last - pos["entry"]) * pos["qty"]
    return gross - cc.fnum(pos.get("fees"), 0.0) - funding_paid


def count_skip(skips, reason):
    skips[reason] = skips.get(reason, 0) + 1


def scan_args(symbols):
    scan = load_mod("crypto-scanner", "scan")
    args = scan.args_from()
    args.setups = "all"
    args.side = "both"
    args.symbols_list = symbols
    args.min_volume = None
    args.top = None
    args.workers = None
    return args


def venue_named(name, cache):
    """The venue recorded on a trade. A later fallback does not rewrite an open position's prices."""
    if name not in cache:
        cache[name] = cc.get_venue(name)
    return cache[name]


def manage_open(positions, now):
    """(still_open, close_events, funding_by_id). Closes are not written here."""
    still = []
    closes = []
    funding = {}
    venues = {}
    for pos in positions:
        try:
            venue = venue_named(pos.get("venue") or "binance", venues)
            bars = venue.bars_between(pos["symbol"], "1m", pos["opened_at"], now)
            fund = funding_events(venue, pos, now)
        except cc.CryptoError:
            still.append(pos)
            funding[pos["id"]] = 0.0
            continue
        closed, paid = walk_position(pos, bars, fund, now)
        funding[pos["id"]] = paid
        if closed is None:
            still.append(pos)
            continue
        price, reason, at, fee_out = closed
        closes.append(close_event(pos, price, reason, at, fee_out, paid))
    return still, closes, funding


def close_event(pos, price, reason, at, fee_out, funding_paid):
    fees_in = cc.fnum(pos.get("fees"), 0.0)
    gross = cc.side_sign(pos["side"]) * (price - pos["entry"]) * pos["qty"]
    pnl = gross - fees_in - fee_out - funding_paid
    risk = pos.get("risk_usd") or abs(pos["entry"] - pos["stop"]) * pos["qty"] or 1e-9
    return {
        "type": "close", "id": pos["id"], "signal_id": pos.get("signal_id"),
        "experiment": pos.get("experiment"), "rules": pos.get("rules"),
        "paper": True, "simulated": True,
        "at": at, "at_utc": cc.fmt_utc(at),
        "opened_at": pos.get("opened_at"), "opened_at_utc": pos.get("opened_at_utc") or cc.fmt_utc(pos.get("opened_at")),
        "setup": pos.get("setup"), "side": pos.get("side"), "symbol": pos.get("symbol"), "coin": pos.get("coin"),
        "venue": pos.get("venue"), "entry": pos.get("entry"), "stop": pos.get("stop"), "target": pos.get("target"),
        "exit": price, "r": pnl / risk, "fees": fees_in + fee_out, "funding": funding_paid,
        "pnl_usd": pnl, "exit_reason": reason, "reason": reason, "qty": pos.get("qty"),
    }


def build_open(order, hit, fill, venue, profile, now):
    costs = order.get("costs") or {}
    taker = costs.get("taker") or 0.0
    slip = costs.get("slippage") or 0.0
    fee = order["qty"] * fill * taker
    risk_coin = abs(fill - order["stop"])
    risk_usd = order["qty"] * risk_coin
    loss = order["qty"] * (risk_coin + (taker + slip) * order["stop"]) + fee
    lev = order["leverage"] or 1
    margin = order["qty"] * fill / lev
    mmr = order.get("mmr") or 0.02
    liq = cc.liq_price(fill, order["side"], lev, mmr, taker)
    reward = abs(order["target"] - fill) / risk_coin if risk_coin else None
    sid = signal_id(hit)
    ident = "p%d-%s" % (now, order["symbol"])
    return {
        "type": "open", "id": ident, "signal_id": sid,
        "experiment": profile.get("experiment"), "rules": profile.get("rules"),
        "paper": True, "simulated": True,
        "at": now, "at_utc": cc.fmt_utc(now),
        "opened_at": now, "opened_at_utc": cc.fmt_utc(now),
        "setup": (order.get("setup") or hit.get("setup")), "side": order["side"],
        "symbol": order["symbol"], "coin": order["coin"], "venue": venue.name,
        "entry": fill, "stop": order["stop"], "target": order["target"],
        "r": reward, "fees": fee, "qty": order["qty"],
        "exit_by": order.get("exit_by"), "exit_reason": None,
        "risk_usd": risk_usd, "loss_at_stop_usd": loss, "margin": margin,
        "leverage": lev, "liq": liq, "mmr": mmr,
        "costs": {"taker": costs.get("taker"), "maker": costs.get("maker"), "slippage": costs.get("slippage")},
        "funding_rate": order.get("funding_rate") or 0.0,
        "funding_hours": order.get("funding_hours") or 8.0,
        "tick": order.get("tick"), "step": order.get("step"),
        "fill_note": None,
    }


def consider_entries(venue, hits, profile, cfg, ctx, taken, now, skips, limits):
    """Plans, sizes and risk-checks fresh hits. Returns open events to append. Writes nothing."""
    exit_mod = load_mod("crypto-exit", "exit")
    size_mod = load_mod("crypto-budget", "size")
    risk_mod = load_mod("crypto-risk", "check")
    params = None
    opened = []
    size_args = _SizeArgs()
    for hit in hits:
        if params is None:
            params = {}
        reason = skip_reason(hit, now, int(profile.get("decision_minutes") or DECISION_MINUTES_DEFAULT))
        if reason:
            count_skip(skips, reason)
            continue
        sid = signal_id(hit)
        if sid in taken:
            count_skip(skips, "already_taken")
            continue
        coin = hit.get("coin")
        if any((p.get("coin") or cc.clean_coin(p.get("symbol") or "")) == coin for p in ctx["positions"]):
            count_skip(skips, "duplicate")
            limits.add("duplicate")
            continue
        if cfg["max_loss_per_trade_usd"] <= 0 or cfg["account_usd"] <= 0:
            count_skip(skips, "no_equity")
            continue
        try:
            market = venue.market(hit["symbol"])
            plan, _words = exit_mod.build_plan(venue, market, hit["side"], cfg, hit=hit, params=hit.get("_params"),
                                               scanned_at=hit.get("_scanned_at"), entry="market")
            order = size_mod.size(plan, cfg, venue, size_args)
        except cc.CryptoError:
            count_skip(skips, "plan")
            continue
        verdict, reasons, _warns, facts = risk_mod.check_order(order, cfg, ctx, venue, market,
                                                              venue.ticker(market["symbol"]), plan, None)
        if verdict != "PASS":
            for text in reasons:
                name = limit_name(text)
                count_skip(skips, name)
                limits.add(name)
            continue
        try:
            fill, note = market_fill(venue, market, order["side"], order["qty"])
        except cc.CryptoError:
            count_skip(skips, "book")
            continue
        sg = cc.side_sign(order["side"])
        if sg * (fill - order["stop"]) <= 0:
            count_skip(skips, "fill_past_stop")
            continue
        pos = build_open(order, hit, fill, venue, profile, now)
        pos["fill_note"] = note
        pos["_loss_for_batch"] = facts.get("loss_usd") if facts.get("loss_usd") is not None else pos["loss_at_stop_usd"]
        pos["_margin_for_batch"] = facts.get("margin") if facts.get("margin") is not None else pos["margin"]
        opened.append(pos)
        taken.add(sid)
        ctx["positions"].append({"symbol": pos["symbol"], "coin": pos["coin"], "venue": pos["venue"],
                                 "margin": pos["_margin_for_batch"], "loss_at_stop_usd": pos["_loss_for_batch"]})
        ctx["entries"] += 1
        ctx["open_risk"] += pos["_loss_for_batch"]
        ctx["margin_used"] += pos["_margin_for_batch"]
        # The next candidate is sized off equity after this fill's fee.
        cfg["account_usd"] = max(0.0, cfg["account_usd"] - pos["fees"])
        cfg["max_loss_per_trade_usd"] = risk_budget_usd(cfg["account_usd"], profile)
    for pos in opened:
        pos.pop("_loss_for_batch", None)
        pos.pop("_margin_for_batch", None)
    return opened


def attach_scan(hits, data):
    params = data.get("params") or {}
    scanned = data.get("as_of")
    for hit in hits:
        hit["_params"] = params
        hit["_scanned_at"] = scanned
    return hits


def marks(positions, funding):
    total = 0.0
    venues = {}
    for pos in positions:
        try:
            last = venue_named(pos.get("venue") or "binance", venues).ticker(pos["symbol"])["last"]
        except cc.CryptoError:
            last = pos["entry"]
        total += unrealized(pos, last, funding.get(pos["id"], 0.0))
    return total


def context_from(events, positions, cfg, now):
    start = cc.day_start(now, cc.risk_day_zone(cfg))
    opens_today = [e for e in events if e.get("type") == "open" and start <= e.get("at", 0) <= now]
    closes = [e for e in events if e.get("type") == "close"]
    closes.sort(key=lambda e: e.get("at", 0))
    rows = []
    for pos in positions:
        rows.append({"symbol": pos.get("symbol"), "coin": pos.get("coin"), "venue": pos.get("venue"),
                     "margin": cc.fnum(pos.get("margin"), 0.0),
                     "loss_at_stop_usd": cc.fnum(pos.get("loss_at_stop_usd"), cc.fnum(pos.get("margin"), 0.0))})
    return {
        "blocked": set(str(x).upper() for x in cfg.get("blocked") or []),
        "positions": rows,
        "entries": len(opens_today),
        "realized": day_pnl(events, start, now),
        "open_risk": sum(p["loss_at_stop_usd"] for p in rows),
        "margin_used": sum(p["margin"] for p in rows),
        "last_closes": closes[-10:],
        "max_drift_r": 0.5,
        "day_start": start,
    }


def header_event(profile, now):
    return {
        "type": "experiment", "at": now, "at_utc": cc.fmt_utc(now),
        "name": profile.get("experiment"), "experiment": profile.get("experiment"),
        "rules": profile.get("rules"), "start_equity_usd": profile.get("start_equity_usd"),
        "note": "Fresh simulated capital for this experiment. Separate from crypto/ledger.jsonl and from any older history.",
        "paper": True, "simulated": True,
    }


def commit(path, events_to_add):
    """Append only. Returns the file's full text from before this commit, for the caller to keep."""
    for event in events_to_add:
        cc.append_jsonl(path, event)


@contextlib.contextmanager
def locked(directory):
    os.makedirs(directory, exist_ok=True)
    handle = open(os.path.join(directory, ".lock"), "a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def execute(cfg, symbols, write):
    profile = paper_profile(cfg)
    now = cc.now_ms()
    directory, path = ledger_paths(profile)
    # Binance USDT-M first, then Bybit, then Hyperliquid. Not the config's pinned venue.
    venue = cc.get_venue("auto")
    scan = load_mod("crypto-scanner", "scan")
    data = scan.compute(scan_args(symbols), venue, experiment_config(cfg, profile, profile.get("start_equity_usd") or 1000))
    hits = attach_scan(list(data.get("hits") or []), data)

    def once():
        events = cc.read_jsonl(path)
        positions = open_positions(events)
        still, closes, funding = manage_open(positions, now)
        # Equity after this run's closes, before new entries.
        preview = list(events) + closes
        equity = start_equity(preview, profile) + realized_pnl(preview) + marks(still, funding)
        run_cfg = experiment_config(cfg, profile, equity)
        ctx = context_from(preview, still, run_cfg, now)
        taken = set(e.get("signal_id") for e in events if e.get("type") == "open" and e.get("signal_id"))
        skips = {}
        limits = set()
        fresh = consider_entries(venue, hits, profile, run_cfg, ctx, taken, now, skips, limits)
        adding = []
        if write and not any(e.get("type") == "experiment" for e in events):
            adding.append(header_event(profile, now))
        # Re-check ids against the ledger we just read. The lock keeps a second run from racing us.
        closed_ids = set(e.get("id") for e in events if e.get("type") == "close")
        open_ids = set(e.get("signal_id") for e in events if e.get("type") == "open")
        new_closes = [c for c in closes if c.get("id") not in closed_ids]
        new_opens = [o for o in fresh if o.get("signal_id") not in open_ids]
        adding.extend(new_closes)
        adding.extend(new_opens)
        if write and adding:
            commit(path, adding)
        final_events = list(events) + adding
        final_open = open_positions(final_events)
        # Funding on positions we did not close is what manage_open just computed.
        final_funding = {}
        for pos in final_open:
            final_funding[pos["id"]] = funding.get(pos["id"], 0.0)
        final_equity = start_equity(final_events, profile) + realized_pnl(final_events) + marks(final_open, final_funding)
        start = cc.day_start(now, cc.risk_day_zone(run_cfg))
        summary = {
            "experiment": profile.get("experiment"),
            "rules": profile.get("rules"),
            "new": len(new_opens),
            "closed": len(new_closes),
            "open": len(final_open),
            "day_pnl": round(day_pnl(final_events, start, now), 2),
            "equity": round(final_equity, 2),
            "limit_hits": sorted(limits),
            "skip_reasons": dict(sorted(skips.items())),
            "venue": venue.name,
            "at_utc": cc.fmt_utc(now),
            "paper": True,
        }
        return summary

    if write:
        with locked(directory):
            return once()
    return once()


def refuse_reason(cfg, live_flag):
    why = []
    if not cfg.get("paper", True):
        why.append('"paper" is false in the config')
    if live_flag:
        why.append("--live was given")
    if not why:
        return None
    return ("auto_paper is paper-only and refuses to run: %s. It never places an order and never reads API keys. "
            "Live orders stay on crypto-broker, which still needs a ticket and your typed yes." % " and ".join(why))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="auto_paper.py",
                                     description="Paper-only syed-v1 simulation. One JSON summary line. Never places an order.")
    parser.add_argument("--symbols", default=None, help="only these coins (default: the liquid universe)")
    parser.add_argument("--live", action="store_true", help="refused: this tool never trades live")
    cc.add_common(parser, venue=False)
    args = parser.parse_args(argv)
    cc.apply_common(args)
    cfg = cc.load_config()
    reason = refuse_reason(cfg, args.live)
    if reason:
        raise cc.CryptoError(reason)
    symbols = [s.strip() for s in (args.symbols or "").split(",") if s.strip()] or None
    summary = execute(cfg, symbols, write=not args.no_write)
    sys.stdout.write(json.dumps(summary, sort_keys=False, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(cc.run(main))
