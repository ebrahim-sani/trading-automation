import { Injectable, OnModuleInit, OnModuleDestroy, Logger } from '@nestjs/common';
import { PrismaClient } from '@prisma/client';

@Injectable()
export class PrismaService extends PrismaClient implements OnModuleInit, OnModuleDestroy {
  private readonly logger = new Logger(PrismaService.name);

  async onModuleInit() {
    try {
      await this.$connect();
      this.logger.log('Successfully connected to MongoDB Atlas');
    } catch (err) {
      this.logger.error(`Failed to connect to MongoDB: ${err.message}`);
      // Don't throw here to allow NestJS to boot, but we'll see it in logs
    }
  }

  async onModuleDestroy() {
    await this.$disconnect();
    this.logger.log('Disconnected from MongoDB');
  }
}
