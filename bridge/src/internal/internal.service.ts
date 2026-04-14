import { Injectable, Logger } from '@nestjs/common';
import { PrismaService } from '../common/prisma.service';

@Injectable()
export class InternalService {
  private readonly logger = new Logger(InternalService.name);

  constructor(private prisma: PrismaService) {}

  // ── Signal logging (v7: stores score + factor breakdown) ──────────────
  async logSignal(data: any) {
    const signal = await this.prisma.signal.create({
      data: {
        ticker:  data.ticker,
        action:  data.action,
        entry:   data.entry,
        sl:      data.sl,
        tp:      data.tp,
        rr:      data.rr,
        bias1h:  data.bias1h  ?? 'HTF',
        bias4h:  data.bias4h  ?? 'HTF',
        aligned: data.aligned ?? true,
        score:   data.score   ?? null,   // v7: 0–100
        factors: data.factors ?? null,   // v7: {trend,sweep,disp,atr,vol}
      },
    });
    this.logger.log(
      `Signal | ${data.ticker} ${data.action.toUpperCase()} | ` +
      `Score: ${data.score ?? '?'}/100 | RR 1:${data.rr?.toFixed(2)}`,
    );
    return signal;
  }

  // ── Trade open ────────────────────────────────────────────────────────
  async openTrade(data: any) {
    const trade = await this.prisma.trade.create({
      data: {
        ticker:       data.ticker,
        action:       data.action,
        entry:        data.entry,
        sl:           data.sl,
        tp:           data.tp,
        lots:         data.lots,
        riskUsd:      data.riskUsd,
        mt5Ticket:    data.mt5Ticket ? String(data.mt5Ticket) : null,
        status:       'OPEN',
        openedAt:     new Date(),
        scoreAtEntry: data.scoreAtEntry ?? null,
        setupScore:   data.setupScore   ?? null,
      },
    });
    this.logger.log(
      `Trade OPEN | #${data.mt5Ticket} | ${data.ticker} ${data.action} ` +
      `${data.lots} lots | Risk $${data.riskUsd} | Score: ${data.scoreAtEntry ?? '?'}`,
    );
    return trade;
  }

  // ── Trade failed ──────────────────────────────────────────────────────
  async failTrade(data: any) {
    const trade = await this.prisma.trade.create({
      data: {
        ticker:   data.ticker,
        action:   data.action,
        entry:    data.entry,
        sl:       data.sl,
        tp:       data.tp,
        lots:     data.lots,
        riskUsd:  data.riskUsd,
        status:   'FAILED',
        errorMsg: data.error,
      },
    });
    this.logger.error(`Trade FAILED | ${data.ticker} | ${data.error}`);
    return trade;
  }

  // ── Trade closed (MT5 ticket stored as string for MongoDB) ────────────
  async closeTrade(data: any) {
    await this.prisma.trade.updateMany({
      where: { mt5Ticket: String(data.mt5Ticket) },
      data: {
        status:      'CLOSED',
        closedAt:    new Date(),
        closeReason: data.reason,
        pnl:         data.pnl,
      },
    });
    this.logger.log(
      `Trade CLOSED | #${data.mt5Ticket} | Reason: ${data.reason} | PnL: $${data.pnl?.toFixed(2)}`,
    );
    return { ok: true };
  }

  // ── Breakeven marker ──────────────────────────────────────────────────
  async setBreakeven(ticket: string) {
    await this.prisma.trade.updateMany({
      where: { mt5Ticket: String(ticket) },
      data:  { breakevenSet: true },
    });
    this.logger.log(`Breakeven set | #${ticket}`);
    return { ok: true };
  }

  // ── Partial close marker (new: +1.5R partial exit) ────────────────────
  async setPartialClosed(ticket: string) {
    await this.prisma.trade.updateMany({
      where: { mt5Ticket: String(ticket) },
      data:  { partialClosed: true },
    });
    this.logger.log(`Partial close recorded | #${ticket}`);
    return { ok: true };
  }

  // ── Open trades list ──────────────────────────────────────────────────
  async getOpenTrades() {
    return this.prisma.trade.findMany({ where: { status: 'OPEN' } });
  }

  // ── Daily PnL ─────────────────────────────────────────────────────────
  async getTodayPnl() {
    const startOfDay = new Date();
    startOfDay.setHours(0, 0, 0, 0);
    const trades = await this.prisma.trade.findMany({
      where: { status: 'CLOSED', closedAt: { gte: startOfDay } },
      select: { pnl: true },
    });
    return {
      pnl:   trades.reduce((sum, t) => sum + (t.pnl ?? 0), 0),
      count: trades.length,
    };
  }

  // ── Weekly PnL (ISO week) ─────────────────────────────────────────────
  async getWeekPnl() {
    const now   = new Date();
    const day   = now.getDay() || 7;                    // Mon=1 … Sun=7
    const startOfWeek = new Date(now);
    startOfWeek.setDate(now.getDate() - (day - 1));     // rewind to Monday
    startOfWeek.setHours(0, 0, 0, 0);

    const trades = await this.prisma.trade.findMany({
      where: { status: 'CLOSED', closedAt: { gte: startOfWeek } },
      select: { pnl: true },
    });
    return {
      pnl:   trades.reduce((sum, t) => sum + (t.pnl ?? 0), 0),
      count: trades.length,
    };
  }
}

