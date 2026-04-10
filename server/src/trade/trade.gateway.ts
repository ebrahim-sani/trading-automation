import {
   WebSocketGateway,
   WebSocketServer,
   OnGatewayConnection,
   OnGatewayDisconnect,
} from "@nestjs/websockets";
import { Server, Socket } from "socket.io";

@WebSocketGateway({ cors: true })
export class TradeGateway implements OnGatewayConnection, OnGatewayDisconnect {
   @WebSocketServer()
   server: Server;

   private clients = new Set<string>();

   handleConnection(client: Socket) {
      const apiKey = client.handshake.headers["x-api-key"];
      if (apiKey !== "test_key_123") {
         console.log("WS: Unauthorized connection attempt");
         client.disconnect();
         return;
      }
      this.clients.add(client.id);
      console.log(
         `WS: Client connected (${client.id}). Total: ${this.clients.size}`,
      );
   }

   handleDisconnect(client: Socket) {
      this.clients.delete(client.id);
      console.log(`WS: Client disconnected. Total: ${this.clients.size}`);
   }

   sendTrade(tradeData: any) {
      console.log("WS: Pushing trade to all connected executors");
      this.server.emit("new_trade", tradeData);
   }
}
