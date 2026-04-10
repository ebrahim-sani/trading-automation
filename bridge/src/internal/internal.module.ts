import { Module } from '@nestjs/common';
import { InternalController } from './internal.controller';
import { InternalService } from './internal.service';
import { PrismaService } from '../common/prisma.service';

@Module({
  controllers: [InternalController],
  providers: [InternalService, PrismaService],
})
export class InternalModule {}
