import { NestFactory } from "@nestjs/core";
import { AppModule } from "./app.module";

async function bootstrap() {
   const app = await NestFactory.create(AppModule);

   // Enable CORS if TradingView needs it (though it's server-to-server)
   app.enableCors();

   await app.listen(3000);
   console.log(`Application is running on: ${await app.getUrl()}`);
}
bootstrap();
