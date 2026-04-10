import {
   Controller,
   Post,
   Body,
   Headers,
   UnauthorizedException,
   UsePipes,
   ValidationPipe,
} from "@nestjs/common";
import { TradeService } from "./trade.service";
import { TradeWebhookDto } from "./dto/trade-webhook.dto";

@Controller("trades")
export class TradeController {
   private readonly SECRET_KEY = "test_key_123";

   constructor(private readonly tradeService: TradeService) {}

   @Post("webhook")
   @UsePipes(new ValidationPipe({ transform: true }))
   handleWebhook(
      @Body() dto: TradeWebhookDto,
      @Headers("x-api-key") apiKey: string,
   ) {
      if (apiKey !== this.SECRET_KEY) {
         throw new UnauthorizedException("Invalid API Key");
      }

      console.log(`WEBHOOK: Received ${dto.action} signal for ${dto.ticker}`);
      return this.tradeService.processTrade(dto);
   }
}
