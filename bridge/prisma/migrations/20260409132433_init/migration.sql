/*
  Warnings:

  - You are about to alter the column `mt5Ticket` on the `Trade` table. The data in that column could be lost. The data in that column will be cast from `Int` to `BigInt`.

*/
-- RedefineTables
PRAGMA defer_foreign_keys=ON;
PRAGMA foreign_keys=OFF;
CREATE TABLE "new_Trade" (
    "id" TEXT NOT NULL PRIMARY KEY,
    "signalId" TEXT,
    "ticker" TEXT NOT NULL,
    "action" TEXT NOT NULL,
    "entry" REAL NOT NULL,
    "sl" REAL NOT NULL,
    "tp" REAL NOT NULL,
    "lots" REAL NOT NULL,
    "riskUsd" REAL NOT NULL DEFAULT 5.0,
    "mt5Ticket" BIGINT,
    "status" TEXT NOT NULL DEFAULT 'PENDING',
    "openedAt" DATETIME,
    "closedAt" DATETIME,
    "closeReason" TEXT,
    "breakevenSet" BOOLEAN NOT NULL DEFAULT false,
    "pnl" REAL,
    "errorMsg" TEXT,
    "createdAt" DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" DATETIME NOT NULL,
    CONSTRAINT "Trade_signalId_fkey" FOREIGN KEY ("signalId") REFERENCES "Signal" ("id") ON DELETE SET NULL ON UPDATE CASCADE
);
INSERT INTO "new_Trade" ("action", "breakevenSet", "closeReason", "closedAt", "createdAt", "entry", "errorMsg", "id", "lots", "mt5Ticket", "openedAt", "pnl", "riskUsd", "signalId", "sl", "status", "ticker", "tp", "updatedAt") SELECT "action", "breakevenSet", "closeReason", "closedAt", "createdAt", "entry", "errorMsg", "id", "lots", "mt5Ticket", "openedAt", "pnl", "riskUsd", "signalId", "sl", "status", "ticker", "tp", "updatedAt" FROM "Trade";
DROP TABLE "Trade";
ALTER TABLE "new_Trade" RENAME TO "Trade";
CREATE UNIQUE INDEX "Trade_signalId_key" ON "Trade"("signalId");
PRAGMA foreign_keys=ON;
PRAGMA defer_foreign_keys=OFF;
