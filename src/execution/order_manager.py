from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple, Mapping
from uuid import UUID

from src.core.logging_config import get_logger
from src.core.models import Position, Signal
from src.core.exceptions import OrderPlacementError
from src.db.repositories.position_repository import PositionRepository
from src.db.repositories.signal_repository import SignalRepository
from src.integration.bybit.rest_client import BybitRESTClient
from src.risk.risk_manager import RiskManager

__all__ = ["PartialFillPolicy", "OrderManager"]

logger = get_logger("execution.order_manager")


@dataclass(frozen=True)
class PartialFillPolicy:
    """
    Политика обработки частичного исполнения ордера.

    По спецификации:
    - если fill_ratio < min_fill_ratio_to_open       → позиция считается не открытой;
    - если min_fill_ratio_to_open ≤ fill_ratio < 0.95:
        * в зависимости от политики ("accept"/"retry") принимаем частичный fill
          или пробуем донабрать объём;
    - если fill_ratio ≥ full_fill_ratio (обычно 0.95) → считаем позицию полностью открытой.

    Здесь мы реализуем исключительно:
    - порог min_fill_ratio_to_open;
    - порог full_fill_ratio (по умолчанию 0.95).
    Выбор "accept"/"retry" оставляем внешнему уровню (API/оркестрация),
    чтобы не завязывать OrderManager на конкретную бизнес-политику.
    """

    min_fill_ratio_to_open: Decimal = Decimal("0.5")   # минимум, чтобы вообще считать позицию открытой
    full_fill_ratio: Decimal = Decimal("0.95")         # с этого порога считаем позицию фактически полной


class OrderManager:
    """
    Менеджер ручного исполнения ордеров.

    Назначение:
    - принять запрос UI (`signal_id`);
    - убедиться, что сигнал ещё "свежий" и не просрочен;
    - повторно спросить RiskManager — можно ли открывать позицию по этому сигналу
      в текущий момент (лимиты, anti-churn и т.п.);
    - создать лимитный ордер через Bybit REST;
    - дождаться исполнения/частичного исполнения (по REST, без WS);
    - в зависимости от fill_ratio:
        * открыть позицию и записать fill_ratio + slippage_entry_bps;
        * либо отменить ордер и вернуть ошибку underfill.

    Обработка ошибок:
    - сетевые/REST-ошибки Bybit оборачиваются в OrderPlacementError;
    - причины отказа по риск-лимитам и экспирации сигнала также оформляются
      как OrderPlacementError, а в сигнал пишутся error_code/error_message.
    """

    def __init__(
        self,
        *,
        bybit_rest_client: BybitRESTClient,
        position_repository: PositionRepository,
        signal_repository: SignalRepository,
        risk_manager: RiskManager,
        partial_fill_policy: Optional[PartialFillPolicy] = None,
        order_timeout: int = 30,
        poll_interval: float = 1.0,
        signal_grace_seconds: int = 5,
    ) -> None:
        """
        :param bybit_rest_client: Низкоуровневый REST-клиент Bybit.
        :param position_repository: Репозиторий позиций.
        :param signal_repository: Репозиторий сигналов.
        :param risk_manager: Централизованный RiskManager.
        :param partial_fill_policy: Политика обработки частичных fill'ов.
        :param order_timeout: Таймаут ожидания исполнения ордера (в секундах).
        :param poll_interval: Интервал между запросами статуса ордера (в секундах).
        :param signal_grace_seconds: Максимальный "возраст" сигнала в секундах.
        """
        self._rest = bybit_rest_client
        self._positions = position_repository
        self._signals = signal_repository
        self._risk = risk_manager
        self._policy = partial_fill_policy or PartialFillPolicy()
        self._order_timeout = order_timeout
        self._poll_interval = poll_interval
        self._signal_grace_seconds = signal_grace_seconds

    # --------------------------------------------------------------------- #
    # Публичный API
    # --------------------------------------------------------------------- #

    async def place_order(
        self,
        signal_id: UUID,
        *,
        now: Optional[datetime] = None,
    ) -> Position:
        """
        Основной метод: открыть позицию по сигналу.

        Алгоритм:
          1. Загружаем сигнал из БД.
          2. Проверяем "свежесть" сигнала.
          3. Проверяем риск-лимиты через RiskManager.
          4. Формируем лимитный ордер и отправляем в Bybit.
          5. Ждём исполнения/частичного исполнения через wait_for_fills(...).
          6. При underfill < min_fill_ratio_to_open — отменяем ордер и кидаем OrderPlacementError.
          7. При достаточном fill_ratio — создаём Position, считаем slippage и сохраняем.

        :param signal_id: ID сигнала, по которому нужно открыть позицию.
        :param now: Текущее время (UTC) — для тестов можно переопределить.
        :return: Созданная Position.
        :raises OrderPlacementError: при любой бизнес-ошибке или проблеме с Bybit.
        """
        if now is None:
            now = datetime.now(timezone.utc)

        # 1. Сигнал
        signal = await self._signals.get_by_id(signal_id)
        if signal is None:
            msg = f"Signal {signal_id} not found"
            logger.warning(msg)
            raise OrderPlacementError(msg, details={"signal_id": str(signal_id)})

        # 2. Свежесть сигнала
        if not self.validate_signal_freshness(signal, now=now):
            msg = "Signal expired for manual order placement"
            logger.info(msg, signal_id=str(signal.id))
            await self._signals.update_error(
                signal.id,
                error_code=None,
                error_message=msg,
            )
            raise OrderPlacementError(msg, details={"signal_id": str(signal.id)})

        # 3. Проверка риск-лимитов (повторная, на момент ручного входа)
        allowed, reason = await self._risk.check_limits(signal, now=now)
        if not allowed:
            msg = f"Order rejected by RiskManager: {reason}"
            logger.info(
                "Risk limits rejected manual order",
                signal_id=str(signal.id),
                reason=reason,
            )
            await self._signals.update_error(
                signal.id,
                error_code=None,
                error_message=msg,
            )
            raise OrderPlacementError(
                msg,
                details={"signal_id": str(signal.id), "reason": reason},
            )

        # 4. Формируем и отправляем ордер в Bybit.
        qty_base = self._compute_order_size(signal)
        side = "Buy" if signal.direction == "long" else "Sell"

        body: Dict[str, Any] = {
            "symbol": signal.symbol,
            "side": side,
            "orderType": "Limit",
            "qty": str(qty_base),
            "price": str(signal.entry_price),
            "timeInForce": "PostOnly",
            "orderLinkId": str(signal.id),
            "reduceOnly": False,
        }

        try:
            create_resp = await self._rest.request(
                "POST",
                "/v5/order/create",
                body=body,
                auth=True,
                is_order=True,
                read_weight=1,
            )
        except Exception as exc:  # noqa: BLE001
            msg = f"Failed to create Bybit order: {exc}"
            logger.exception(
                "Failed to create Bybit order",
                signal_id=str(signal.id),
            )
            await self._signals.update_error(
                signal.id,
                error_code=None,
                error_message=msg,
            )
            raise OrderPlacementError(
                msg,
                details={"signal_id": str(signal.id)},
            ) from exc

        order_id = self._extract_order_id(create_resp)
        if not order_id:
            msg = "Bybit create_order response missing orderId"
            logger.error(msg, response=create_resp)
            await self._signals.update_error(
                signal.id,
                error_code=None,
                error_message=msg,
            )
            raise OrderPlacementError(
                msg,
                details={"signal_id": str(signal.id), "response": create_resp},
            )

        # 5. Ждём fills.
        fill_ratio, avg_price, status = await self.wait_for_fills(
            order_id=order_id,
            symbol=signal.symbol,
        )

        # 6. Underfill — не открываем позицию.
        if fill_ratio < self._policy.min_fill_ratio_to_open:
            logger.info(
                "Underfill for manual order, cancelling",
                signal_id=str(signal.id),
                order_id=order_id,
                fill_ratio=str(fill_ratio),
                status=status,
            )
            await self._cancel_order(signal.symbol, order_id)

            msg = f"Order underfilled: fill_ratio={fill_ratio}"
            await self._signals.update_error(
                signal.id,
                error_code=None,
                error_message=msg,
            )
            raise OrderPlacementError(
                msg,
                details={
                    "signal_id": str(signal.id),
                    "order_id": order_id,
                    "fill_ratio": str(fill_ratio),
                    "status": status,
                },
            )

        # 7. Fill достаточный для открытия позиции — создаём Position.
        position = await self._create_position_from_fill(
            signal=signal,
            fill_ratio=fill_ratio,
            avg_price=avg_price,
            opened_at=now,
        )

        logger.info(
            "Manual position opened",
            position_id=str(position.id),
            signal_id=str(position.signal_id),
            symbol=position.symbol,
            direction=position.direction,
            fill_ratio=str(position.fill_ratio),
            slippage_bps=str(position.slippage),
        )

        return position

    # --------------------------------------------------------------------- #
    # Проверка свежести сигнала
    # --------------------------------------------------------------------- #

    def validate_signal_freshness(self, signal: Signal, *, now: datetime) -> bool:
        """
        Проверить, что сигнал ещё годен для ручного открытия позиции.

        По спецификации: допускается небольшой grace-интервал (по умолчанию 5 секунд)
        между генерацией сигнала и его использованием UI.

        :param signal: Модель Signal.
        :param now: Текущее время (UTC).
        :return: True, если сигнал достаточно свежий.
        """
        age = now - signal.created_at
        return age <= timedelta(seconds=self._signal_grace_seconds)

    # --------------------------------------------------------------------- #
    # Ожидание исполнения ордера
    # --------------------------------------------------------------------- #

    async def wait_for_fills(
        self,
        *,
        order_id: str,
        symbol: str,
    ) -> Tuple[Decimal, Decimal, str]:
        """
        Подождать исполнения ордера по REST, опрашивая Bybit.

        По факту реализует то, что в спецификации обозначено как
        `bybit_rest_client.get_order`:
        - периодически запрашивает состояние ордера;
        - следит за cumExecQty/qty и orderStatus;
        - останавливается при:
            * статусах FILLED/CANCELED/REJECTED;
            * достижении таймаута.

        :param order_id: Идентификатор ордера на стороне Bybit.
        :param symbol: Торговый инструмент.
        :return: (fill_ratio, avg_price, status).
        :raises TimeoutError: если ордер так и не перешёл в финальное состояние.
        :raises OrderPlacementError: при ошибках API.
        """
        deadline = datetime.now(timezone.utc) + timedelta(seconds=self._order_timeout)
        last_status: str = "NEW"
        last_ratio = Decimal("0")
        last_avg_price = Decimal("0")

        while datetime.now(timezone.utc) < deadline:
            try:
                resp = await self._rest.request(
                    "GET",
                    "/v5/order/realtime",
                    params={"symbol": symbol, "orderId": order_id},
                    auth=True,
                    is_order=True,
                    read_weight=1,
                )
            except Exception as exc:  # noqa: BLE001
                msg = f"Failed to query Bybit order status: {exc}"
                logger.exception(
                    "Failed to query Bybit order status",
                    order_id=order_id,
                    symbol=symbol,
                )
                raise OrderPlacementError(
                    msg,
                    details={"order_id": order_id, "symbol": symbol},
                ) from exc

            order_data = self._extract_order_data(resp)
            if order_data is None:
                logger.warning(
                    "Bybit order status response missing data, continuing",
                    response=resp,
                )
                await asyncio.sleep(self._poll_interval)
                continue

            qty = self._to_decimal(order_data.get("qty") or order_data.get("orderQty"))
            cum = self._to_decimal(order_data.get("cumExecQty"))
            avg_price = self._to_decimal(
                order_data.get("avgPrice") or order_data.get("price")
            )
            status = str(order_data.get("orderStatus") or "").upper()

            if qty is not None and qty > 0 and cum is not None:
                fill_ratio = max(Decimal("0"), min(cum / qty, Decimal("1")))
            else:
                fill_ratio = last_ratio

            last_status = status or last_status
            last_ratio = fill_ratio
            last_avg_price = avg_price or last_avg_price

            # Финальные статусы ордера.
            if status in {"FILLED", "CANCELED", "REJECTED"}:
                break

            # Если fill_ratio уже достигло "почти полного" значения — выходим.
            if fill_ratio >= self._policy.full_fill_ratio:
                break

            await asyncio.sleep(self._poll_interval)

        if last_status not in {"FILLED", "CANCELED", "REJECTED"} and last_ratio < self._policy.full_fill_ratio:
            # Таймаут: ордер завис в промежуточном состоянии.
            msg = f"Timeout while waiting for fills (status={last_status}, fill_ratio={last_ratio})"
            logger.warning(
                "Timeout waiting for Bybit order fills",
                order_id=order_id,
                symbol=symbol,
                status=last_status,
                fill_ratio=str(last_ratio),
            )
            raise TimeoutError(msg)

        return last_ratio, last_avg_price, last_status

    # --------------------------------------------------------------------- #
    # Запись деталей fill в Position
    # --------------------------------------------------------------------- #

    async def _create_position_from_fill(
        self,
        *,
        signal: Signal,
        fill_ratio: Decimal,
        avg_price: Decimal,
        opened_at: datetime,
    ) -> Position:
        """
        Создать Position по факту исполнения ордера и записать slippage.

        slippage_entry_bps считается направленным:
        - для long:  (actual / expected - 1) * 10_000;
        - для short: (expected / actual - 1) * 10_000.
        """
        if avg_price <= 0:
            raise OrderPlacementError(
                "avg_price must be positive to create position",
                details={"signal_id": str(signal.id), "avg_price": str(avg_price)},
            )

        # Фактический размер позиции по fill_ratio.
        nominal_size_base = signal.stake_usd / signal.entry_price
        size_base = (nominal_size_base * fill_ratio).copy_abs()
        size_quote = (size_base * avg_price).copy_abs()

        slippage_bps = self._compute_directional_slippage_bps(
            direction=signal.direction,
            expected_price=signal.entry_price,
            actual_price=avg_price,
        )

        position = Position(
            signal_id=signal.id,
            opened_at=opened_at,
            closed_at=None,
            symbol=signal.symbol,
            direction=signal.direction,
            entry_price=avg_price,
            size_base=size_base,
            size_quote=size_quote,
            fill_ratio=fill_ratio,
            slippage=slippage_bps,
            funding=Decimal("0"),
        )

        # Сохраняем в БД.
        return await self._positions.create(position)

    # --------------------------------------------------------------------- #
    # Вспомогательные методы для Bybit / чисел
    # --------------------------------------------------------------------- #

    def _compute_order_size(self, signal: Signal) -> Decimal:
        """
        Рассчитать размер ордера в базовой валюте по stake_usd и entry_price.
        """
        if signal.entry_price <= 0:
            raise OrderPlacementError(
                "Signal entry_price must be positive",
                details={"signal_id": str(signal.id), "entry_price": str(signal.entry_price)},
            )
        qty = (signal.stake_usd / signal.entry_price).copy_abs()
        if qty <= 0:
            raise OrderPlacementError(
                "Computed order qty is non-positive",
                details={"signal_id": str(signal.id), "qty": str(qty)},
            )
        return qty

    @staticmethod
    def _extract_order_id(resp: Mapping[str, Any]) -> Optional[str]:
        """
        Извлечь orderId из ответа Bybit `POST /v5/order/create`.

        Ожидаемый формат:
            {"result": {"orderId": "...", ...}, ...}
        """
        result = resp.get("result") if isinstance(resp, dict) else None
        if isinstance(result, dict):
            order_id = result.get("orderId")
            if order_id:
                return str(order_id)
        return None

    @staticmethod
    def _extract_order_data(resp: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Извлечь описание ордера из ответа Bybit `GET /v5/order/realtime`.

        Ожидаемый формат:
            {"result": {"list": [ {...}, ... ]}, ...}
        Берём первый элемент `list` как агрегированный view ордера.
        """
        if not isinstance(resp, dict):
            return None

        result = resp.get("result")
        if not isinstance(result, dict):
            return None

        lst = result.get("list")
        if isinstance(lst, list) and lst:
            first = lst[0]
            if isinstance(first, dict):
                return first

        # Иногда API может вернуть просто dict без "list".
        if isinstance(result, dict):
            return result

        return None

    @staticmethod
    def _to_decimal(value: Any) -> Optional[Decimal]:
        """
        Мягко преобразовать значение к Decimal.

        При None или непарсимом значении возвращает None.
        """
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _compute_directional_slippage_bps(
        *,
        direction: str,
        expected_price: Decimal,
        actual_price: Decimal,
    ) -> Decimal:
        """
        Посчитать направленный slippage в bps.

        Для long:
            slippage_bps = (actual / expected - 1) * 10_000
            (положительное значение — хуже, чем планировалось).
        Для short:
            slippage_bps = (expected / actual - 1) * 10_000.
        """
        if expected_price <= 0 or actual_price <= 0:
            raise OrderPlacementError(
                "Prices must be positive for slippage calculation",
                details={
                    "expected_price": str(expected_price),
                    "actual_price": str(actual_price),
                },
            )

        if direction == "long":
            return (actual_price / expected_price - Decimal("1")) * Decimal("10000")
        elif direction == "short":
            return (expected_price / actual_price - Decimal("1")) * Decimal("10000")
        else:
            raise OrderPlacementError(
                f"Unsupported direction for slippage: {direction!r}",
                details={"direction": direction},
            )

    async def _cancel_order(self, symbol: str, order_id: str) -> None:
        """
        Попробовать отменить ордер в Bybit.

        Ошибки логируются, но не выбрасываются дальше: если отмена не удалась,
        дальнейшее поведение совпадает (позиция всё равно не будет создана).
        """
        try:
            await self._rest.request(
                "POST",
                "/v5/order/cancel",
                body={"symbol": symbol, "orderId": order_id},
                auth=True,
                is_order=True,
                read_weight=1,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to cancel Bybit order after underfill",
                symbol=symbol,
                order_id=order_id,
                error=str(exc),
            )
