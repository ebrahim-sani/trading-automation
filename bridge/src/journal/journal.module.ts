import { Module } from '@nestjs/common';
import { JournalController } from './journal.controller';
import { JournalService } from './journal.service';
import { PrismaService } from '../common/prisma.service';

@Module({
  controllers: [JournalController],
  providers: [JournalService, PrismaService],
})
export class JournalModule {}
