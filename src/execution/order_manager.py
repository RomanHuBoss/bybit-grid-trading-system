from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from src.core.exceptions import DatabaseError, ExecutionError, NetworkError
from src.core.logging_config import get_logger
from src.core.models import Position, Signal
from src.db.repositories.position_repository import PositionRepository
from src.integration.bybit.rest_client import BybitRESTClient

logger = get_logger("execution.order_manager")


@dataclass(frozen=True)
class OrderResult:
    """
    Результат постановки ордера на биржу.

    Это тонкая обёртка над ответом Bybit, которая вытаскивает только то,
    что нужно нашей бизнес-логике для создания Position.
    """

    order_id: str
    symbol: str
    side: str
    qty: Decimal
    avg_price: Decimal


class OrderManager:
    """
    Исполнитель ордеров стратегии AVI-5 через Bybit REST API.

    Отвечает за:
      - трансляцию доменной модели Signal → параметры ордера Bybit;
      - вызов BybitRESTClient и обработку ошибок (сеть / бизнес-коды);
      - построение доменной Position и сохранение её в БД.
    """

    def __init__(
        self,
        rest_client: BybitRESTClient,
        position_repository: PositionRepository,
        *,
        category: str = "linear",
        default_leverage: int = 1,
    ) -> None:
        self._rest = rest_client
        self._positions = position_repository
        self._category = category
        self._default_leverage = default_leverage

        logger.debug(
            "OrderManager initialized",
            category=self._category,
            default_leverage=self._default_leverage,
        )

    # ------------------------------------------------------------------ #
    # Публичный API                                                      #
    # ------------------------------------------------------------------ #

    async def open_position(self, signal: Signal) -> Position:
        """
        Открыть позицию по сигналу.

        Шаги:
          1. Рассчитать размер позиции по stake_usd и entry_price.
          2. Отправить market-ордер на Bybit.
          3. Сконструировать Position по фактическому fill'у.
          4. Сохранить Position в БД.
        """
        logger.info(
            "Opening position for signal",
            signal_id=str(signal.id),
            symbol=signal.symbol,
            direction=signal.direction,
            stake_usd=str(signal.stake_usd),
        )

        try:
            order_result = await self._place_market_order(signal)
        except NetworkError as exc:
            # Сеть / инфраструктура — это ExecutionError для верхнего уровня.
            raise ExecutionError(
                "Failed to place order on Bybit due to network error",
                details={"signal_id": str(signal.id), "error": str(exc)},
            ) from exc
        except ExecutionError:
            # Уже ExecutionError — просто пробрасываем.
            raise

        position = await self._build_and_persist_position(signal, order_result)

        logger.info(
            "Position opened",
            position_id=str(position.id),
            signal_id=str(signal.id),
            symbol=position.symbol,
            direction=position.direction,
            entry_price=str(position.entry_price),
            size_base=str(position.size_base),
            size_quote=str(position.size_quote),
        )

        return position

    # ------------------------------------------------------------------ #
    # Внутренние помощники                                               #
    # ------------------------------------------------------------------ #

    async def _place_market_order(self, signal: Signal) -> OrderResult:
        """
        Поставить простой market-ордер на Bybit по сигналу.

        Используем REST-эндпоинт v5/order/create.
        """
        side = self._direction_to_side(signal.direction)

        # Считаем размер позиции.
        stake_usd = Decimal(signal.stake_usd)
        entry_price = Decimal(signal.entry_price)

        if entry_price <= 0:
            raise ExecutionError(
                "Signal entry_price must be positive to open position",
                details={"signal_id": str(signal.id), "entry_price": str(entry_price)},
            )

        # Кол-во базового актива; точность потом можно подтюнить под биржу.
        size_base = (stake_usd / entry_price).quantize(Decimal("0.0001"))

        # Формируем тело запроса по требованиям Bybit v5.
        body: dict[str, Any] = {
            "category": self._category,
            "symbol": signal.symbol,
            "side": side,  # "Buy" / "Sell"
            "orderType": "Market",
            "qty": str(size_base),
            "timeInForce": "IOC",
            "reduceOnly": False,
            "closeOnTrigger": False,
            "positionIdx": 0,
        }

        if self._default_leverage > 0:
            body["leverage"] = str(self._default_leverage)

        logger.debug(
            "Sending Bybit create-order request",
            symbol=signal.symbol,
            side=side,
            body=body,
        )

        try:
            data = await self._rest.request(
                method="POST",
                path="v5/order/create",
                body=body,
                auth=True,
                is_order=True,
            )
        except NetworkError:
            # Прозрачно пробрасываем вверх.
            raise

        # Ожидаем формат v5: {"retCode":0, "retMsg":"OK", "result":{"orderId": "...", ...}}
        result = data.get("result") or {}
        order_id = result.get("orderId")
        if not order_id:
            raise ExecutionError(
                "Bybit did not return orderId in create-order response",
                details={"response": data},
            )

        # Bybit может вернуть avgPrice и cumExecQty после исполнения market-ордеров.
        avg_price_raw = result.get("avgPrice") or result.get("price") or "0"
        cum_exec_qty_raw = result.get("cumExecQty") or result.get("qty") or "0"

        try:
            avg_price = Decimal(str(avg_price_raw))
            cum_exec_qty = Decimal(str(cum_exec_qty_raw))
        except (ArithmeticError, ValueError) as exc:
            raise ExecutionError(
                "Failed to parse numeric fields from Bybit order response",
                details={
                    "avgPrice": avg_price_raw,
                    "cumExecQty": cum_exec_qty_raw,
                    "response": result,
                },
            ) from exc

        if cum_exec_qty <= 0:
            # Ордер не был исполнен — для простоты считаем это ошибкой исполнения.
            raise ExecutionError(
                "Bybit order was not filled",
                details={
                    "order_id": order_id,
                    "avg_price": str(avg_price),
                    "cum_exec_qty": str(cum_exec_qty),
                },
            )

        return OrderResult(
            order_id=order_id,
            symbol=signal.symbol,
            side=side,
            qty=cum_exec_qty,
            avg_price=avg_price,
        )

    async def _build_and_persist_position(
        self,
        signal: Signal,
        order_result: OrderResult,
    ) -> Position:
        """
        Создать Position по факту исполнения ордера и сохранить её в БД.
        """
        now = datetime.now(timezone.utc)

        # Расчёт size_quote и fill_ratio по фактическому исполнению.
        size_base = order_result.qty
        size_quote = (order_result.qty * order_result.avg_price).quantize(Decimal("0.01"))
        requested_stake_usd = Decimal(signal.stake_usd)
        fill_ratio = (
            (size_quote / requested_stake_usd)
            if requested_stake_usd > 0
            else Decimal("1")
        )

        # Используем dict[str, Any] → **kwargs, как и в PositionRepository,
        # чтобы не конфликтовать с строгой сигнатурой Position.__init__ в mypy.
        position_data: dict[str, Any] = {
            "id": uuid4(),
            "signal_id": signal.id if isinstance(signal.id, UUID) else UUID(str(signal.id)),
            "symbol": order_result.symbol,
            "direction": signal.direction,
            "entry_price": order_result.avg_price,
            "size_base": size_base,
            "size_quote": size_quote,
            "fill_ratio": fill_ratio,
            "opened_at": now,
            "closed_at": None,
            # поля, которые присутствуют в модели, но ещё не накоплены
            "funding": Decimal("0"),
            "slippage": Decimal("0"),
        }

        position = Position(**position_data)

        try:
            saved = await self._positions.create(position)
        except DatabaseError as exc:
            # Если не смогли сохранить позицию — это серьёзно, хотя ордер уже стоит на бирже.
            logger.error(
                "Failed to persist opened position",
                signal_id=str(signal.id),
                position_id=str(position.id),
                error=str(exc),
            )
            raise ExecutionError(
                "Failed to persist opened position after successful order",
                details={
                    "signal_id": str(signal.id),
                    "position_id": str(position.id),
                    "order_id": order_result.order_id,
                },
            ) from exc

        return saved

    @staticmethod
    def _direction_to_side(direction: str) -> str:
        """
        Конвертация доменного направления ("long"/"short") в Bybit side ("Buy"/"Sell").
        """
        normalized = direction.lower()
        if normalized == "long":
            return "Buy"
        if normalized == "short":
            return "Sell"
        raise ExecutionError(
            "Unsupported direction for Bybit side",
            details={"direction": direction},
        )
