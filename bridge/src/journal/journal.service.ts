import { Injectable } from '@nestjs/common';
import { PrismaService } from '../common/prisma.service';

@Injectable()
export class JournalService {
  constructor(private prisma: PrismaService) {}

  async getStats() {
    const trades = await this.prisma.trade.findMany({
      where:   { status: 'CLOSED' },
      orderBy: { closedAt: 'desc' },
    });

    const wins   = trades.filter((t) => (t.pnl ?? 0) > 0);
    const losses = trades.filter((t) => (t.pnl ?? 0) <= 0);
    const totalPnl = trades.reduce((s, t) => s + (t.pnl ?? 0), 0);
    const avgWin   = wins.length   ? wins.reduce((s, t) => s + (t.pnl ?? 0), 0)   / wins.length   : 0;
    const avgLoss  = losses.length ? Math.abs(losses.reduce((s, t) => s + (t.pnl ?? 0), 0) / losses.length) : 0;

    return {
      totalTrades:  trades.length,
      wins:         wins.length,
      losses:       losses.length,
      winRate:      trades.length ? `${((wins.length / trades.length) * 100).toFixed(1)}%` : 'N/A',
      totalPnl:     `$${totalPnl.toFixed(2)}`,
      avgWin:       `$${avgWin.toFixed(2)}`,
      avgLoss:      `$${avgLoss.toFixed(2)}`,
      profitFactor: avgLoss ? (avgWin / avgLoss).toFixed(2) : 'N/A',
      closeReasons: trades.reduce<Record<string, number>>((acc, t) => {
        const r = t.closeReason ?? 'unknown';
        acc[r] = (acc[r] ?? 0) + 1;
        return acc;
      }, {}),
      recent: trades.slice(0, 15).map((t) => ({
        ticket:   t.mt5Ticket,
        ticker:   t.ticker,
        action:   t.action,
        lots:     t.lots,
        pnl:      t.pnl,
        reason:   t.closeReason,
        duration: t.openedAt && t.closedAt
          ? `${Math.round((t.closedAt.getTime() - t.openedAt.getTime()) / 60000)}min`
          : null,
      })),
    };
  }

  async getFilterImpact() {
    const aligned = await this.prisma.signal.count({ where: { aligned: true } });
    const blocked = await this.prisma.signal.count({ where: { aligned: false } });
    const total   = aligned + blocked;

    const closedTrades = await this.prisma.trade.findMany({ where: { status: 'CLOSED' } });
    const wins = closedTrades.filter((t) => (t.pnl ?? 0) > 0);

    return {
      totalSignals:   total,
      alignedSignals: aligned,
      blockedSignals: blocked,
      blockedPct:     total ? `${((blocked / total) * 100).toFixed(1)}%` : '0%',
      alignedWinRate: closedTrades.length
        ? `${((wins.length / closedTrades.length) * 100).toFixed(1)}%`
        : 'N/A',
      insight: `The 4H filter blocked ${blocked} of ${total} total signals (${total ? ((blocked / total) * 100).toFixed(0) : 0}%). These would have been taken at the current win rate, potentially costing ~$${(blocked * (1 - (wins.length / Math.max(closedTrades.length, 1))) * 5).toFixed(2)}.`,
    };
  }

  async getSignalLog(limit = 50) {
    return this.prisma.signal.findMany({
      take:    limit,
      orderBy: { receivedAt: 'desc' },
      include: {
        trades: { select: { status: true, pnl: true, closeReason: true } },
      },
    });
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
