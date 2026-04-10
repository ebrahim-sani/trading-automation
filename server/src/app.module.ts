import { Module } from '@nestjs/common';
import { TradeController } from './trade/trade.controller';
import { TradeService } from './trade/trade.service';
import { TradeGateway } from './trade/trade.gateway';

@Module({
  imports: [],
  controllers: [TradeController],
  providers: [TradeService, TradeGateway],
})
export class AppModule {}
