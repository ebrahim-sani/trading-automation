import { NestFactory } from '@nestjs/core';
import { AppModule } from './app.module';

// BigInt is not serializable by JSON.stringify by default.
// MT5 ticket numbers exceed 32-bit INT range so we store them as BigInt.
// This patches JSON globally to serialize BigInt as a plain number string.
(BigInt.prototype as any).toJSON = function () {
  return this.toString();
};

async function bootstrap() {
  const app = await NestFactory.create(AppModule);
  app.enableCors();
  const port = process.env.PORT ?? 3000;
  await app.listen(port);
  console.log(`TTFM Bridge running on http://localhost:${port}`);
  console.log(`Journal:  http://localhost:${port}/journal/stats`);
}
bootstrap();
