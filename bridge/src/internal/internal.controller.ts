import { Controller, Post, Body, Get, Param, UseGuards } from '@nestjs/common';
import { InternalService } from './internal.service';
import { ApiKeyGuard } from '../common/guards/api-key.guard';

@Controller('internal')
@UseGuards(ApiKeyGuard)
export class InternalController {
  constructor(private svc: InternalService) {}

  @Post('signal')
  logSignal(@Body() body: any) {
    return this.svc.logSignal(body);
  }

  @Post('trade/open')
  openTrade(@Body() body: any) {
    return this.svc.openTrade(body);
  }

  @Post('trade/fail')
  failTrade(@Body() body: any) {
    return this.svc.failTrade(body);
  }

  @Post('trade/close')
  closeTrade(@Body() body: any) {
    return this.svc.closeTrade(body);
  }

  @Post('trade/:ticket/breakeven')
  setBreakeven(@Param('ticket') ticket: string) {
    return this.svc.setBreakeven(ticket);
  }

  @Post('trade/:ticket/partial-close')
  setPartialClosed(@Param('ticket') ticket: string) {
    return this.svc.setPartialClosed(ticket);
  }

  @Get('trade/open')
  getOpenTrades() {
    return this.svc.getOpenTrades();
  }

  @Get('today-pnl')
  getTodayPnl() {
    return this.svc.getTodayPnl();
  }

  @Get('week-pnl')
  getWeekPnl() {
    return this.svc.getWeekPnl();
  }
}
