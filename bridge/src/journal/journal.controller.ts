import { Controller, Get, Query } from '@nestjs/common';
import { JournalService } from './journal.service';

@Controller('journal')
export class JournalController {
  constructor(private svc: JournalService) {}

  @Get('stats')
  getStats() {
    return this.svc.getStats();
  }

  @Get('filter-impact')
  getFilterImpact() {
    return this.svc.getFilterImpact();
  }

  @Get('signals')
  getSignals(@Query('limit') limit?: string) {
    return this.svc.getSignalLog(limit ? parseInt(limit) : 50);
  }
}
