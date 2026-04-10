import { IsString, IsNumber, IsIn, IsOptional } from 'class-validator';

export class TradeWebhookDto {
  @IsString()
  ticker: string;

  @IsString()
  @IsIn(['buy', 'sell'])
  action: 'buy' | 'sell';

  @IsNumber()
  price: number;

  @IsNumber()
  sl: number;

  @IsNumber()
  tp2: number;

  @IsOptional()
  @IsNumber()
  risk_usd?: number;
}
