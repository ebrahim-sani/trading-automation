import { Injectable, Logger } from '@nestjs/common';
import { PrismaService } from '../common/prisma.service';

@Injectable()
export class InternalService {
  private readonly logger = new Logger(InternalService.name);

  constructor(private prisma: PrismaService) {}

  async logSignal(data: any) {
    const signal = await this.prisma.signal.create({
      data: {
        ticker:  data.ticker,
        action:  data.action,
        entry:   data.entry,
        sl:      data.sl,
        tp:      data.tp,
        rr:      data.rr,
        bias1h:  data.bias1h,
        bias4h:  data.bias4h,
        aligned: data.aligned,
      },
    });
    this.logger.log(
      `Signal | ${data.ticker} ${data.action.toUpperCase()} | RR 1:${data.rr?.toFixed(2)} | Aligned: ${data.aligned}`
    );
    return signal;
  }

  async openTrade(data: any) {
    const trade = await this.prisma.trade.create({
      data: {
        ticker:    data.ticker,
        action:    data.action,
        entry:     data.entry,
        sl:        data.sl,
        tp:        data.tp,
        lots:      data.lots,
        riskUsd:   data.riskUsd,
        mt5Ticket: data.mt5Ticket ? BigInt(data.mt5Ticket) : null,
        status:    'OPEN',
        openedAt:  new Date(),
      },
    });
    this.logger.log(
      `Trade OPEN | #${data.mt5Ticket} | ${data.ticker} ${data.action} ${data.lots} lots | Risk $${data.riskUsd}`
    );
    return trade;
  }

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

  async closeTrade(data: any) {
    await this.prisma.trade.updateMany({
      where: { mt5Ticket: BigInt(data.mt5Ticket) },
      data: {
        status:      'CLOSED',
        closedAt:    new Date(),
        closeReason: data.reason,
        pnl:         data.pnl,
      },
    });
    this.logger.log(
      `Trade CLOSED | #${data.mt5Ticket} | Reason: ${data.reason} | PnL: $${data.pnl?.toFixed(2)}`
    );
    return { ok: true };
  }

  async setBreakeven(ticket: bigint | number | string) {
    await this.prisma.trade.updateMany({
      where: { mt5Ticket: BigInt(ticket) },
      data:  { breakevenSet: true },
    });
    this.logger.log(`Breakeven confirmed | #${ticket}`);
    return { ok: true };
  }

  async getOpenTrades() {
    return this.prisma.trade.findMany({ where: { status: 'OPEN' } });
  }

  async getTodayPnl() {
    const startOfDay = new Date();
    startOfDay.setHours(0, 0, 0, 0);

    const trades = await this.prisma.trade.findMany({
      where: {
        status: 'CLOSED',
        closedAt: { gte: startOfDay },
      },
      select: { pnl: true },
    });

    return {
      pnl: trades.reduce((sum, t) => sum + (t.pnl ?? 0), 0),
      count: trades.length,
    };
  }
}
