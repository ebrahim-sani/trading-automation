import { Injectable } from '@nestjs/common';
import { TradeGateway } from './trade.gateway';
import { TradeWebhookDto } from './dto/trade-webhook.dto';

@Injectable()
export class TradeService {
  private processedTrades = new Set<string>();

  constructor(private readonly gateway: TradeGateway) {}

  calculateLots(entry: number, sl: number, riskUsd: number = 5): number {
    const pipsAtRisk = Math.abs(entry - sl) / 0.0001; 
    if (pipsAtRisk === 0) return 0.01;
    const lotSize = riskUsd / (pipsAtRisk * 10);
    return Math.max(0.01, Math.round(lotSize * 100) / 100);
  }

  async processTrade(dto: TradeWebhookDto) {
    // 1. Idempotency Check (Prevent duplicate TradingView alerts)
    const requestId = `${dto.ticker}-${dto.action}-${dto.price}-${Math.floor(Date.now() / 30000)}`;
    if (this.processedTrades.has(requestId)) {
      console.log(`Trade ${requestId} already processed. Skipping.`);
      return { status: 'duplicate', skipped: true };
    }
    this.processedTrades.add(requestId);

    // 2. Calculate Risk
    const lots = this.calculateLots(dto.price, dto.sl, dto.risk_usd ?? 5);

    const fullTrade = {
      ...dto,
      lots,
      id: requestId,
      timestamp: new Date().toISOString()
    };

    // 3. Push to Python Executor via WebSocket
    this.gateway.sendTrade(fullTrade);

    return { status: 'executed', lots, trade_id: requestId };
  }
}
