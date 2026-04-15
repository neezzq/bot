# MRKT Alert Bot

Телеграм-бот для отслеживания новых выставлений на MRKT по фильтрам.

## Что умеет
- следить за новыми выставлениями на MRKT;
- фильтровать по названиям подарков (`/set_collections`);
- фильтровать по моделям (`/set_models`);
- фильтровать по диапазону цены (`/set_price`);
- показывать статистику по текущим лотам (`/stats`): минимум, средняя цена выставления, медиана.

## Ограничение
Этот бот считает статистику по текущим лотам из `gifts/saling`. История завершённых сделок в приложенном описании API не показана, поэтому точную среднюю цену реальных покупок/продаж он пока не считает.

## Запуск
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# заполни .env
export $(grep -v '^#' .env | xargs)
python bot.py
```

## Команды
- `/start`
- `/help`
- `/filters`
- `/set_collections часы, кепки`
- `/set_models gold, albino`
- `/set_price 1 20`
- `/stats`
- `/on`
- `/off`
