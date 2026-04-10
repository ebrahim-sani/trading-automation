-- CreateTable
CREATE TABLE "Signal" (
    "id" TEXT NOT NULL PRIMARY KEY,
    "ticker" TEXT NOT NULL,
    "action" TEXT NOT NULL,
    "entry" REAL NOT NULL,
    "sl" REAL NOT NULL,
    "tp" REAL NOT NULL,
    "rr" REAL NOT NULL,
    "bias1h" TEXT NOT NULL,
    "bias4h" TEXT NOT NULL,
    "aligned" BOOLEAN NOT NULL,
    "receivedAt" DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- CreateTable
CREATE TABLE "Trade" (
    "id" TEXT NOT NULL PRIMARY KEY,
    "signalId" TEXT,
    "ticker" TEXT NOT NULL,
    "action" TEXT NOT NULL,
    "entry" REAL NOT NULL,
    "sl" REAL NOT NULL,
    "tp" REAL NOT NULL,
    "lots" REAL NOT NULL,
    "riskUsd" REAL NOT NULL DEFAULT 5.0,
    "mt5Ticket" INTEGER,
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

-- CreateIndex
CREATE UNIQUE INDEX "Trade_signalId_key" ON "Trade"("signalId");
