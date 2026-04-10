import { Module } from '@nestjs/common';
import { ConfigModule } from '@nestjs/config';
import { ScheduleModule } from '@nestjs/schedule';
import { InternalModule } from './internal/internal.module';
import { JournalModule } from './journal/journal.module';
import { PrismaService } from './common/prisma.service';

@Module({
  imports: [
    ConfigModule.forRoot({ isGlobal: true }),
    ScheduleModule.forRoot(),
    InternalModule,
    JournalModule,
  ],
  providers: [PrismaService],
})
export class AppModule {}
