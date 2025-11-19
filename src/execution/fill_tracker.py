from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, Optional
from uuid import UUID

from src.core.logging_config import get_logger
from src.core.models import Position
from src.db.repositories.position_repository import PositionRepository
from src.execution.slippage_monitor import SlippageMonitor
from src.integration.bybit.ws_client import BybitWSClient

__all__ = ["FillTracker"]

logger = get_logger("execution.fill_tracker")


class FillTracker:
    """
    Трекер исполнений ордеров по приватному потоку Bybit `user.order`.

    Задачи:
    - слушать WebSocket-поток `user.order` через BybitWSClient;
    - на каждом fill-событии обновлять fill_ratio позиции в БД;
    - для закрывающих ордеров (reduceOnly) прокидывать данные в SlippageMonitor
      и помечать позицию как закрытую.

    ВАЖНО:
    - FillTracker сам по себе не открывает позиции и не создаёт ордера;
      он только синхронизирует фактические исполнения с уже существующими Position.
    - Подразумевается, что orderLinkId на стороне Bybit равен UUID сигнала,
      по которому была создана позиция (Position.signal_id).
    """

    def __init__(
        self,
        *,
        ws_client: BybitWSClient,
        position_repository: PositionRepository,
        slippage_monitor: SlippageMonitor,
    ) -> None:
        self._ws = ws_client
        self._positions = position_repository
        self._slippage = slippage_monitor

    # --------------------------------------------------------------------- #
    # Публичный API
    # --------------------------------------------------------------------- #

    async def run(self) -> None:
        """
        Основной цикл обработки событий `user.order`.

        Подписывается на приватный поток и затем бесконечно читает события
        через BybitWSClient.listen(). Обработка каждого события уходит
        в отдельную таску, чтобы не блокировать WS-loop.
        """
        # Гарантируем подписку на приватный поток user.order
        await self._ws.subscribe_user_data()

        async for channel, data, sequence in self._ws.listen():
            if channel != "user.order":
                # В теории на этот клиент могут быть подписаны и другие каналы.
                continue

            try:
                for row in self._iter_order_events(data):
                    # Не блокируем чтение WebSocket'а тяжёлыми операциями БД.
                    # Каждое fill-событие обрабатывается в отдельной таске.
                    import asyncio

                    asyncio.create_task(self._handle_order_event(row, sequence))
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Failed to dispatch user.order event",
                    error=str(exc),
                    sequence=sequence,
                )

    # --------------------------------------------------------------------- #
    # Внутренняя обработка событий
    # --------------------------------------------------------------------- #

    @staticmethod
    def _iter_order_events(data: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
        """
        Унифицировать формат user.order-сообщения.

        Bybit обычно присылает:
            {"topic": "user.order", "data": [ {...}, {...} ]}

        В _normalize_payload клиент уже обернул это в data-словарь, поэтому:
        - если есть ключ "data" и там список — итерируемся по нему;
        - если "data" — словарь — считаем его одним событием;
        - иначе считаем, что пришёл один "плоский" словарь.
        """
        if "data" in data:
            payload = data["data"]
            if isinstance(payload, list):
                for row in payload:
                    if isinstance(row, dict):
                        yield row
            elif isinstance(payload, dict):
                yield payload
        elif isinstance(data, dict):
            # На всякий случай поддерживаем вариант без вложенного "data".
            yield data

    async def _handle_order_event(self, event: Dict[str, Any], sequence: int) -> None:
        """
        Обработать одно нормализованное событие user.order.

        1. Отфильтровывает не-fill события.
        2. Находит связанную позицию по signal_id (orderLinkId).
        3. Обновляет fill_ratio.
        4. Для reduceOnly-ордеров при полном исполнении:
           - записывает exit-slippage через SlippageMonitor;
           - помечает позицию закрытой.
        """
        # 1) Отфильтровываем неинтересные статусы.
        if not self._is_fill_event(event):
            return

        # 2) Определяем сигнал и связанные позиции.
        signal_id = self._extract_signal_id(event)
        if signal_id is None:
            logger.debug(
                "user.order event without valid orderLinkId, skipping",
                event=event,
                sequence=sequence,
            )
            return

        positions = await self._positions.list_by_signal(signal_id)
        if not positions:
            logger.warning(
                "No positions found for signal_id from user.order",
                signal_id=str(signal_id),
                sequence=sequence,
            )
            return

        # В текущем дизайне ожидается одна позиция на сигнал,
        # но формально list_by_signal возвращает список.
        position = positions[0]

        # 3) Обновляем fill_ratio (включая частичные исполнения).
        updated_position = await self._update_fill_ratio(position, event)

        # 4) Если это reduceOnly-ордер и он полностью исполнен — считаем выход.
        if self._is_reduce_only(event) and self._is_fully_filled(event):
            await self._handle_exit_fill(updated_position, event)

    # --------------------------------------------------------------------- #
    # Признаки fill-событий и reduceOnly
    # --------------------------------------------------------------------- #

    @staticmethod
    def _is_fill_event(event: Dict[str, Any]) -> bool:
        """
        Определить, является ли событие фактическим fill'ом.

        Логика мягкая и не завязана на конкретный формат:
        - наличие execQty или cumExecQty, отличных от нуля, трактуем как fill.
        """
        exec_qty = event.get("execQty") or event.get("cumExecQty")
        if exec_qty is None:
            return False

        try:
            q = Decimal(str(exec_qty))
        except Exception:  # noqa: BLE001
            return False

        return q > 0

    @staticmethod
    def _is_fully_filled(event: Dict[str, Any]) -> bool:
        """
        Определить, считается ли ордер полностью исполненным.

        Ориентируемся на orderStatus и сравнение cumExecQty с qty.
        """
        status = (event.get("orderStatus") or event.get("order_status") or "").upper()
        if status in {"FILLED", "CLOSED"}:
            return True

        qty = event.get("qty") or event.get("orderQty")
        cum = event.get("cumExecQty")
        try:
            qty_d = Decimal(str(qty)) if qty is not None else None
            cum_d = Decimal(str(cum)) if cum is not None else None
        except Exception:  # noqa: BLE001
            return False

        if qty_d is None or cum_d is None:
            return False

        # Считаем полный fill, если cumExecQty примерно равен qty.
        return cum_d >= qty_d

    @staticmethod
    def _is_reduce_only(event: Dict[str, Any]) -> bool:
        """
        Признак того, что ордер является reduce-only (закрывающим позицию).

        Bybit шлёт поле reduceOnly: true/false для таких ордеров.
        """
        return bool(event.get("reduceOnly"))

    # --------------------------------------------------------------------- #
    # Привязка к Position / Signal
    # --------------------------------------------------------------------- #

    @staticmethod
    def _extract_signal_id(event: Dict[str, Any]) -> Optional[UUID]:
        """
        Попытаться извлечь UUID сигнала из orderLinkId.

        Предполагается, что при постановке ордера execution-слой передаёт
        orderLinkId = str(signal.id).
        """
        link_id = event.get("orderLinkId") or event.get("order_link_id")
        if not link_id:
            return None

        try:
            return UUID(str(link_id))
        except Exception:  # noqa: BLE001
            return None

    async def _update_fill_ratio(self, position: Position, event: Dict[str, Any]) -> Position:
        """
        Обновить fill_ratio позиции на основе текущего состояния ордера.

        fill_ratio = min(cumExecQty / qty, 1), если оба значения доступны.
        При ошибках парсинга оставляем fill_ratio без изменений.
        """
        qty = event.get("qty") or event.get("orderQty")
        cum = event.get("cumExecQty")

        if qty is None or cum is None:
            # Нечего пересчитывать.
            return position

        try:
            qty_d = Decimal(str(qty))
            cum_d = Decimal(str(cum))
        except Exception:  # noqa: BLE001
            logger.warning(
                "Failed to parse qty/cumExecQty from user.order event",
                event=event,
                position_id=str(position.id),
            )
            return position

        if qty_d <= 0:
            return position

        new_fill_ratio = cum_d / qty_d
        if new_fill_ratio < 0:
            new_fill_ratio = Decimal("0")
        if new_fill_ratio > 1:
            new_fill_ratio = Decimal("1")

        if new_fill_ratio == position.fill_ratio:
            return position

        position.fill_ratio = new_fill_ratio

        logger.info(
            "Updated position fill_ratio from user.order",
            position_id=str(position.id),
            signal_id=str(position.signal_id),
            symbol=position.symbol,
            direction=position.direction,
            fill_ratio=str(new_fill_ratio),
        )

        return await self._positions.update(position)

    # --------------------------------------------------------------------- #
    # Обработка закрывающих ордеров и запись проскальзывания
    # --------------------------------------------------------------------- #

    async def _handle_exit_fill(self, position: Position, event: Dict[str, Any]) -> None:
        """
        Обработать полностью исполненный reduceOnly-ордер (закрытие позиции):

        1. Посчитать и записать exit-slippage через SlippageMonitor.
        2. Пометить позицию закрытой (closed_at).
        """
        executed_at = self._extract_event_time(event)

        # Пытаемся определить ожидаемую и фактическую цену:
        # - requested_price: limit/trigger цена ордера;
        # - actual_price: avgPrice исполнения.
        requested_raw = event.get("price") or event.get("triggerPrice")
        actual_raw = event.get("avgPrice") or event.get("lastPrice") or requested_raw

        if requested_raw is None or actual_raw is None:
            logger.warning(
                "Exit fill without usable price fields, skipping slippage calculation",
                position_id=str(position.id),
                event=event,
            )
        else:
            try:
                requested_price = Decimal(str(requested_raw))
                actual_price = Decimal(str(actual_raw))
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Failed to parse price fields from exit fill",
                    position_id=str(position.id),
                    event=event,
                )
            else:
                # Размер ATR/глубины тут не считаем — это может сделать
                # более верхний слой, если нужно. Передаём None.
                await self._slippage.record_exit_slippage(
                    position=position,
                    requested_price=requested_price,
                    actual_price=actual_price,
                    atr_percentile=None,
                    depth_usd=None,
                    executed_at=executed_at,
                )

        # Помечаем позицию закрытой.
        closed = await self._positions.mark_closed(position.id, closed_at=executed_at)

        logger.info(
            "Position closed from user.order exit fill",
            position_id=str(position.id),
            closed_at=executed_at.isoformat(),
            closed_exists=closed is not None,
        )

    @staticmethod
    def _extract_event_time(event: Dict[str, Any]) -> datetime:
        """
        Извлечь метку времени исполнения из события.

        Bybit обычно использует миллисекундные timestamps в строковом формате:
        - execTime / updatedTime / createdTime.

        Если все варианты отсутствуют или не парсятся — возвращается текущий UTC.
        """
        raw = event.get("execTime") or event.get("updatedTime") or event.get("createdTime")
        if raw is None:
            return datetime.now(timezone.utc)

        try:
            s = str(raw)
            # Простейшая эвристика: если это целое число длиной > 10, считаем миллисекундами.
            if s.isdigit() and len(s) > 10:
                ts = int(s) / 1000.0
            else:
                ts = float(s)
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except Exception:  # noqa: BLE001
            return datetime.now(timezone.utc)
