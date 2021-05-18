from datetime import datetime
from decimal import Decimal
from typing import Mapping, NamedTuple

from pancaketrade.network import Network
from pancaketrade.persistence import Order, db
from pancaketrade.utils.config import Config
from pancaketrade.utils.generic import chat_message, check_chat_id
from pancaketrade.watchers import OrderWatcher, TokenWatcher
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    Filters,
    MessageHandler,
)
from web3 import Web3


class CreateOrderResponses(NamedTuple):
    TYPE: int = 0
    TRAILING: int = 1
    PRICE: int = 2
    AMOUNT: int = 3
    SLIPPAGE: int = 4
    GAS: int = 5
    SUMMARY: int = 6


class CreateOrderConversation:
    def __init__(self, parent, config: Config):
        self.parent = parent
        self.net: Network = parent.net
        self.config = config
        self.next = CreateOrderResponses()
        self.handler = ConversationHandler(
            entry_points=[CallbackQueryHandler(self.command_createorder, pattern='^create_order:0x[a-fA-F0-9]{40}$')],
            states={
                self.next.TYPE: [CallbackQueryHandler(self.command_createorder_type, pattern='^[^:]*$')],
                self.next.TRAILING: [
                    CallbackQueryHandler(self.command_createorder_trailing, pattern='^[^:]*$'),
                    MessageHandler(Filters.text & ~Filters.command, self.command_createorder_trailing),
                ],
                self.next.PRICE: [
                    CallbackQueryHandler(self.command_createorder_price, pattern='^[^:]*$'),
                    MessageHandler(Filters.text & ~Filters.command, self.command_createorder_price),
                ],
                self.next.AMOUNT: [
                    CallbackQueryHandler(self.command_createorder_amount, pattern='^[^:]*$'),
                    MessageHandler(Filters.text & ~Filters.command, self.command_createorder_amount),
                ],
                self.next.SLIPPAGE: [
                    CallbackQueryHandler(self.command_createorder_slippage, pattern='^[^:]*$'),
                    MessageHandler(Filters.text & ~Filters.command, self.command_createorder_slippage),
                ],
                self.next.GAS: [
                    CallbackQueryHandler(self.command_createorder_gas, pattern='^[^:]*$'),
                    MessageHandler(Filters.text & ~Filters.command, self.command_createorder_gas),
                ],
                self.next.SUMMARY: [
                    CallbackQueryHandler(self.command_createorder_summary, pattern='^[^:]*$'),
                ],
            },
            fallbacks=[CommandHandler('cancelorder', self.command_cancelorder)],
            name='createorder_conversation',
            conversation_timeout=600,
        )

    @check_chat_id
    def command_createorder(self, update: Update, context: CallbackContext):
        assert update.callback_query and context.user_data is not None
        query = update.callback_query
        # query.answer()
        assert query.data
        token_address = query.data.split(':')[1]
        if not Web3.isChecksumAddress(token_address):
            self.command_error(update, context, text='Invalid token address.')
            return ConversationHandler.END
        token = self.parent.watchers[token_address]
        context.user_data['createorder'] = {'token_address': token_address}
        reply_markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton('Stop loss sell', callback_data='stop_loss'),
                    InlineKeyboardButton('Take profit sell', callback_data='limit_sell'),
                ],
                [
                    InlineKeyboardButton('Limit buy', callback_data='limit_buy'),
                    InlineKeyboardButton('❌ Cancel', callback_data='cancel'),
                ],
            ]
        )
        chat_message(
            update,
            context,
            text=f'Creating order for token {token.name}.\nWhich <u>type of order</u> would you like to create?',
            reply_markup=reply_markup,
            edit=False,
        )
        return self.next.TYPE

    @check_chat_id
    def command_createorder_type(self, update: Update, context: CallbackContext):
        assert update.callback_query and context.user_data is not None
        query = update.callback_query
        # query.answer()
        if query.data == 'cancel':
            self.cancel_command(update, context)
            return ConversationHandler.END
        order = context.user_data['createorder']
        if query.data == 'stop_loss':
            order['type'] = 'sell'
            order['above'] = False  # below
            order['trailing_stop'] = None
            # we don't use trailing stop loss here
            token = self.parent.watchers[order['token_address']]
            current_price, _ = self.net.get_token_price(
                token_address=token.address, token_decimals=token.decimals, sell=True
            )
            chat_message(
                update,
                context,
                text='OK, the order will sell as soon as the price is below target price.\n'
                + f'Next, please indicate the <u>price in <b>BNB per {token.symbol}</b></u> '
                + 'at which the order will activate.\n'
                + f'You can use scientific notation like <code>{current_price:.1E}</code> if you want.\n'
                + f'Current price: <b>{current_price:.6g}</b> BNB per {token.symbol}.',
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('❌ Cancel', callback_data='cancel')]]),
            )
            return self.next.PRICE
        elif query.data == 'limit_sell':
            order['type'] = 'sell'
            order['above'] = True  # above
        elif query.data == 'limit_buy':
            order['type'] = 'buy'
            order['above'] = False  # below
        else:
            self.command_error(update, context, text='That type of order is not supported.')
            return ConversationHandler.END
        reply_markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton('1%', callback_data='1'),
                    InlineKeyboardButton('2%', callback_data='2'),
                    InlineKeyboardButton('5%', callback_data='5'),
                    InlineKeyboardButton('10%', callback_data='10'),
                ],
                [
                    InlineKeyboardButton('No trailing stop loss', callback_data='None'),
                    InlineKeyboardButton('❌ Cancel', callback_data='cancel'),
                ],
            ]
        )
        chat_message(
            update,
            context,
            text=f'OK, the order will {order["type"]} when price is '
            + f'{"above" if order["above"] else "below"} target price.\n'
            + 'Do you want to enable <u>trailing stop loss</u>? If yes, what is the callback rate?\n'
            + 'You can also message me a custom value in percent.',
            reply_markup=reply_markup,
        )
        return self.next.TRAILING

    @check_chat_id
    def command_createorder_trailing(self, update: Update, context: CallbackContext):
        assert update.effective_chat and context.user_data is not None
        order = context.user_data['createorder']
        token = self.parent.watchers[order['token_address']]
        current_price, _ = self.net.get_token_price(
            token_address=token.address, token_decimals=token.decimals, sell=order['type'] == 'sell'
        )
        if update.message is None:
            assert update.callback_query
            query = update.callback_query
            # query.answer()
            assert query.data
            if query.data == 'cancel':
                self.cancel_command(update, context)
                return ConversationHandler.END
            if query.data == 'None':
                order['trailing_stop'] = None
                chat_message(
                    update,
                    context,
                    text='OK, the order will use no trailing stop loss.\n'
                    + f'Next, please indicate the <u>price in <b>BNB per {token.symbol}</b></u> '
                    + 'at which the order will activate.\n'
                    + f'You can use scientific notation like <code>{current_price:.1E}</code> if you want.\n'
                    + f'Current price: <b>{current_price:.6g}</b> BNB per {token.symbol}.',
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('❌ Cancel', callback_data='cancel')]]),
                )
                return self.next.PRICE
            try:
                callback_rate = int(query.data)
            except ValueError:
                self.command_error(update, context, text='The callback rate is not recognized.')
                return ConversationHandler.END
        else:
            assert update.message and update.message.text
            try:
                callback_rate = int(update.message.text.strip())
            except ValueError:
                chat_message(update, context, text='⚠️ The callback rate is not recognized, try again:')
                return self.next.TRAILING
        order['trailing_stop'] = callback_rate
        chat_message(
            update,
            context,
            text=f'OK, the order will use trailing stop loss with {callback_rate}% callback.\n'
            + f'Next, please indicate the <u>price in <b>BNB per {token.symbol}</b></u> '
            + 'at which the order will activate.\n'
            + f'You can use scientific notation like <code>{current_price:.1E}</code> if you want.\n'
            + f'Current price: <b>{current_price:.6g}</b> BNB per {token.symbol}.',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('❌ Cancel', callback_data='cancel')]]),
        )
        return self.next.PRICE

    @check_chat_id
    def command_createorder_price(self, update: Update, context: CallbackContext):
        assert update.effective_chat and context.user_data is not None
        order = context.user_data['createorder']
        if update.message is None:  # we got a cancel callback
            # assert update.callback_query
            # query = update.callback_query
            # query.answer()
            self.cancel_command(update, context)
            return ConversationHandler.END
        assert update.message and update.message.text
        try:
            price = Decimal(update.message.text.strip())
        except Exception:
            chat_message(update, context, text='⚠️ The price you inserted is not valid. Try again:')
            return self.next.PRICE
        token = self.parent.watchers[order['token_address']]
        order['limit_price'] = str(price)
        unit = 'BNB' if order['type'] == 'buy' else token.symbol
        balance = (
            self.net.get_bnb_balance()
            if order['type'] == 'buy'
            else self.net.get_token_balance(token_address=token.address)
        )
        # if selling tokens, add options 25/50/75/100% with buttons
        reply_markup = (
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton('25%', callback_data='0.25'),
                        InlineKeyboardButton('50%', callback_data='0.5'),
                        InlineKeyboardButton('75%', callback_data='0.75'),
                        InlineKeyboardButton('100%', callback_data='1.0'),
                    ],
                    [
                        InlineKeyboardButton('❌ Cancel', callback_data='cancel'),
                    ],
                ]
            )
            if order['type'] == 'sell'
            else InlineKeyboardMarkup([[InlineKeyboardButton('❌ Cancel', callback_data='cancel')]])
        )
        chat_message(
            update,
            context,
            text=f'OK, I will {order["type"]} when the price of {token.symbol} reaches {price:.6g} BNB per token.\n'
            + f'Next, <u>how much {unit}</u> do you want me to use for {order["type"]}ing?\n'
            + f'You can use scientific notation like <code>{balance:.1E}</code> if you want.\n'
            + f'Current balance: <b>{balance:.6g} {unit}</b>',
            reply_markup=reply_markup,
        )
        return self.next.AMOUNT

    @check_chat_id
    def command_createorder_amount(self, update: Update, context: CallbackContext):
        assert update.effective_chat and context.user_data is not None
        order = context.user_data['createorder']
        token = self.parent.watchers[order['token_address']]
        if update.message is None:  # we got a button callback, either cancel or fraction of balance
            assert update.callback_query
            query = update.callback_query
            # query.answer()
            if query.data == 'cancel':
                self.cancel_command(update, context)
                return ConversationHandler.END
            assert query.data is not None
            try:
                balance_fraction = Decimal(query.data)
            except Exception:
                self.command_error(update, context, text='The callback rate is not recognized.')
                return ConversationHandler.END
            balance = self.net.get_token_balance(token_address=token.address)
            amount = balance_fraction * balance
        else:
            assert update.message and update.message.text
            try:
                amount = Decimal(update.message.text.strip())
            except Exception:
                chat_message(update, context, text='⚠️ The amount you inserted is not valid. Try again:')
                return self.next.AMOUNT
        decimals = 18 if order['type'] == 'buy' else token.decimals
        bnb_price = self.net.get_bnb_price()
        limit_price = Decimal(order["limit_price"])
        amount_formatted = (
            f'{amount:.6g}' if order['type'] == 'buy' else f'{amount:,.1f}'
        )  # tokens are display in float
        usd_amount = bnb_price * amount if order['type'] == 'buy' else bnb_price * limit_price * amount
        unit = f'BNB worth of {token.symbol}' if order['type'] == 'buy' else token.symbol
        order['amount'] = str(int(amount * Decimal(10 ** decimals)))
        reply_markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        f'{token.default_slippage}% (default)', callback_data=str(token.default_slippage)
                    ),
                    InlineKeyboardButton('1%', callback_data='1'),
                    InlineKeyboardButton('2%', callback_data='2'),
                    InlineKeyboardButton('5%', callback_data='5'),
                ],
                [
                    InlineKeyboardButton('10%', callback_data='10'),
                    InlineKeyboardButton('12%', callback_data='12'),
                    InlineKeyboardButton('15%', callback_data='15'),
                    InlineKeyboardButton('20%', callback_data='20'),
                ],
                [
                    InlineKeyboardButton('❌ Cancel', callback_data='cancel'),
                ],
            ]
        )

        chat_message(
            update,
            context,
            text=f'OK, I will {order["type"]} {amount_formatted} {unit} (~${usd_amount:.2f}) when the condition is '
            + 'reached.\n'
            + 'Next, please indicate the <u>slippage in percent</u> you want to use for this order.\n'
            + 'You can also message me a custom value in percent.',
            reply_markup=reply_markup,
        )
        return self.next.SLIPPAGE

    @check_chat_id
    def command_createorder_slippage(self, update: Update, context: CallbackContext):
        assert update.effective_chat and context.user_data is not None
        order = context.user_data['createorder']
        if update.message is None:
            assert update.callback_query
            query = update.callback_query
            # query.answer()
            assert query.data
            if query.data == 'cancel':
                self.cancel_command(update, context)
                return ConversationHandler.END
            try:
                slippage_percent = int(query.data)
            except ValueError:
                self.command_error(update, context, text='The slippage is not recognized.')
                return ConversationHandler.END
        else:
            assert update.message and update.message.text
            try:
                slippage_percent = int(update.message.text.strip())
            except ValueError:
                chat_message(update, context, text='⚠️ The slippage is not recognized, try again:', edit=False)
                return self.next.SLIPPAGE
        order['slippage'] = slippage_percent
        network_gas_price = Decimal(self.net.w3.eth.gas_price) / Decimal(10 ** 9)
        chat_message(
            update,
            context,
            text=f'OK, the order will use slippage of {slippage_percent}%.\n'
            + 'Finally, please indicate the <u>gas price in Gwei</u> for this order.\n'
            + 'Choose "Default" to use the default network price at the moment '
            + f'of the transaction (currently {network_gas_price:.1g} Gwei) '
            + 'or message me the value.',
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton('network default', callback_data='None'),
                        InlineKeyboardButton('default + 0.1 Gwei', callback_data='+0.1'),
                    ],
                    [
                        InlineKeyboardButton('default + 1 Gwei', callback_data='+1'),
                        InlineKeyboardButton('default + 2 Gwei', callback_data='+2'),
                    ],
                    [InlineKeyboardButton('❌ Cancel', callback_data='cancel')],
                ]
            ),
        )
        return self.next.GAS

    @check_chat_id
    def command_createorder_gas(self, update: Update, context: CallbackContext):
        assert update.effective_chat and context.user_data is not None
        order = context.user_data['createorder']
        if update.message is None:
            assert update.callback_query
            query = update.callback_query
            # query.answer()
            assert query.data
            if query.data == 'cancel':
                self.cancel_command(update, context)
                return ConversationHandler.END
            elif query.data == 'None':
                order['gas_price'] = None
                chat_message(
                    update, context, text='OK, the order will use default network gas price.\nConfirm the order below!'
                )
            elif query.data.startswith('+'):
                try:
                    Decimal(query.data)
                except Exception:
                    self.command_error(update, context, text='Invalid gas price.')
                    return ConversationHandler.END
                order['gas_price'] = query.data
                chat_message(
                    update,
                    context,
                    text=f'OK, the order will use default network gas price {query.data} Gwei.\n'
                    + 'Confirm the order below!',
                )
            else:
                self.command_error(update, context, text='Invalid gas price.')
                return ConversationHandler.END
            return self.print_summary(update, context)
        else:
            assert update.message and update.message.text
            try:
                gas_price_gwei = Decimal(update.message.text.strip())
            except ValueError:
                chat_message(update, context, text='⚠️ The gas price is not recognized, try again:', edit=False)
                return self.next.GAS
        order['gas_price'] = str(Web3.toWei(gas_price_gwei, unit='gwei'))
        chat_message(
            update,
            context,
            text=f'OK, the order will use {gas_price_gwei:.6g} Gwei for gas price.\n<u>Confirm</u> the order below!',
        )
        return self.print_summary(update, context)

    def print_summary(self, update: Update, context: CallbackContext):
        assert update.effective_chat and context.user_data is not None
        order = context.user_data['createorder']
        token = self.parent.watchers[order['token_address']]
        type_name = self.get_type_name(order)
        comparision = self.get_comparison_symbol(order)
        amount = self.get_human_amount(order, token)
        amount_formatted = (
            f'{amount:.6g}' if order['type'] == 'buy' else f'{amount:,.1f}'
        )  # tokens are displayed in float
        unit = self.get_amount_unit(order, token)
        trailing = (
            f'Trailing stop loss {order["trailing_stop"]}% callback\n' if order["trailing_stop"] is not None else ''
        )
        gas_price = (
            f'{Decimal(order["gas_price"]) / Decimal(10 ** 9):.1g} Gwei'
            if order["gas_price"] and not order["gas_price"].startswith('+')
            else 'network default'
            if order["gas_price"] is None
            else f'network default {order["gas_price"]} Gwei'
        )
        limit_price = Decimal(order["limit_price"])
        bnb_price = self.net.get_bnb_price()
        usd_amount = bnb_price * amount if order['type'] == 'buy' else bnb_price * limit_price * amount
        message = (
            '<u>Preview:</u>\n'
            + f'{token.name} - {type_name}\n'
            + trailing
            + f'Amount: {amount_formatted} {unit} (${usd_amount:.2f})\n'
            + f'Price {comparision} {limit_price:.3g} BNB per token\n'
            + f'Slippage: {order["slippage"]}%\n'
            + f'Gas: {gas_price}'
        )
        chat_message(
            update,
            context,
            text=message,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton('✅ Validate', callback_data='ok'),
                        InlineKeyboardButton('❌ Cancel', callback_data='cancel'),
                    ]
                ]
            ),
        )
        return self.next.SUMMARY

    @check_chat_id
    def command_createorder_summary(self, update: Update, context: CallbackContext):
        assert update.effective_chat and update.callback_query and context.user_data is not None
        query = update.callback_query
        # query.answer()
        if query.data != 'ok':
            self.cancel_command(update, context)
            return ConversationHandler.END
        add = context.user_data['createorder']
        token: TokenWatcher = self.parent.watchers[add['token_address']]
        del add['token_address']  # not needed in order record creation
        try:
            db.connect()
            with db.atomic():
                order_record = Order.create(token=token.token_record, created=datetime.now(), **add)
        except Exception as e:
            self.command_error(update, context, text=f'Failed to create database record: {e}')
            return ConversationHandler.END
        finally:
            del context.user_data['createorder']
            db.close()
        order = OrderWatcher(
            order_record=order_record, net=self.net, dispatcher=context.dispatcher, chat_id=update.effective_chat.id
        )
        token.orders.append(order)
        chat_message(update, context, text='✅ Order was added successfully!')
        for job in token.scheduler.get_jobs():  # check prices now
            job.modify(next_run_time=datetime.now())
        return ConversationHandler.END

    @check_chat_id
    def command_cancelorder(self, update: Update, context: CallbackContext):
        self.cancel_command(update, context)
        return ConversationHandler.END

    def get_type_name(self, order: Mapping) -> str:
        return (
            'limit buy'
            if order['type'] == 'buy' and not order['above']
            else 'stop loss'
            if order['type'] == 'sell' and not order['above']
            else 'limit sell'
            if order['type'] == 'sell' and order['above']
            else 'unknown'
        )

    def get_comparison_symbol(self, order: Mapping) -> str:
        return '&gt;' if order['above'] else '&lt;'

    def get_human_amount(self, order: Mapping, token) -> Decimal:
        decimals = token.decimals if order['type'] == 'sell' else 18
        return Decimal(order['amount']) / Decimal(10 ** decimals)

    def get_amount_unit(self, order: Mapping, token) -> str:
        return token.symbol if order['type'] == 'sell' else 'BNB'

    def cancel_command(self, update: Update, context: CallbackContext):
        assert context.user_data is not None
        del context.user_data['createorder']
        chat_message(update, context, text='⚠️ OK, I\'m cancelling this command.', edit=False)

    def command_error(self, update: Update, context: CallbackContext, text: str):
        assert context.user_data is not None
        del context.user_data['createorder']
        chat_message(update, context, text=f'⛔️ {text}', edit=False)
