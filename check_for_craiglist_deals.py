'''
based on https://github.com/kkrypt0nn/Python-Discord-Bot-Template

This is a Discord bot that checks craigslist for new deals and price drops on items you are interested in.

The bot reads the craigslist urls from configs/craigslist_deals_to_check.csv and the known deals from configs/known_deals.csv

This bot will not message more than once per hour and it will only message the first new deal or price drop it finds (so multiple changes can take multiple hours to message through).

Best to make sure your url is one that's specific, like search only a specific area or a very specific search term.

you can add a new deal to check by adding a new row to craigslist_deals_to_check.csv with the following columns:
friendly_name (anything without commas),url (the craigslist url)

For more information, check https://winfred.com/projects/check_for_craiglist_deals/
'''
import os
import platform
import datetime
import asyncio
import requests
import csv
from re import sub
from decimal import Decimal
from bs4 import BeautifulSoup
import discord
from discord.ext import commands, tasks
from discord.ext.commands import Context
from dotenv import load_dotenv
import signal
import time
from myhelperfunctions import logger,sigterm_handler

import functools
import typing

signal.signal(signal.SIGTERM, sigterm_handler)
my_logger = logger(log_filepath='logs/check_for_craiglist_deals.log', logger_name='check_for_craiglist_deals',debug=True)
my_logger.info("starting up")

intents = discord.Intents.default()

# =============================================================================
def to_thread(func: typing.Callable) -> typing.Coroutine:
    '''
    This is a helper function to run blocking functions in a separate thread to prevent the "Heartbeat blocked for more than 10 seconds" warning
    https://stackoverflow.com/questions/65881761/discord-gateway-warning-shard-id-none-heartbeat-blocked-for-more-than-10-second
    '''
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        return await asyncio.to_thread(func, *args, **kwargs)
    return wrapper

# =============================================================================
def check_for_new_deals(friendly_name, url, known_deals):
    '''
    This function checks for new deals on a craigslist page and
    updates the known_deals file with the current prices
    '''
    discord_messages = []
    known_deals_urls = [row['url'] for row in known_deals]
    while True:
        try:
            page = requests.get(url, timeout=30)
            break
        except requests.exceptions.RequestException as e:
            my_logger.error("Requests get Error: " + str(e))
            return
    soup = BeautifulSoup(page.content, "html.parser")
    items = soup.find_all('li', class_='cl-static-search-result')
    for item in items:
        alink = item.find('a')
        item_url = alink['href']
        subitems = alink.find_all('div', class_='price')
        titles = alink.find_all('div', class_='title')
        title = titles[0].text
        if len(subitems) > 0:
            money = subitems[0].text
            price = int(Decimal(sub(r'[^\d.]', '', money)))
        if item_url not in known_deals_urls:
            my_logger.info("found a new deal for " + friendly_name + " at " + item_url)
            with open('configs/known_deals.csv', 'a', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=['title', 'original_price', 'current_price', 'url'],quotechar='"', quoting=csv.QUOTE_MINIMAL)
                writer.writerow({'url': item_url, 'original_price': price, 'current_price': price, 'title': title})
            discord_message = "Found a new craigslist deal for " + friendly_name + ": " + item_url + " [all deals](" + url + ")"
            discord_messages.append(discord_message)
    my_logger.info("no new deals found for " + friendly_name)
    
    # Now check the prices of the known deals:
    for item in items:
        alink = item.find('a')
        item_url = alink['href']
        subitems = alink.find_all('div', class_='price')
        titles = alink.find_all('div', class_='title')
        title = titles[0].text
        if len(subitems) > 0:
            money = subitems[0].text
            price = int(Decimal(sub(r'[^\d.]', '', money)))
        for known_deal in known_deals:
            if known_deal['url'] == item_url:
                if int(known_deal['current_price']) > price:
                    my_logger.info("price drop for " + title + " to " + str(price) + " from " + str(known_deal['current_price']) + " [deal](" + item_url + ")")
                    discord_message = "Price drop for " + title + " to " + str(price) + " from " + str(known_deal['current_price']) + " [deal](" + item_url + ")" + " [all deals](" + url + ")"
                    known_deal['current_price'] = price
                    with open('configs/known_deals.csv', 'w', newline='') as f:
                        writer = csv.DictWriter(f, fieldnames=['title', 'original_price', 'current_price', 'url'],quotechar='"', quoting=csv.QUOTE_MINIMAL)
                        writer.writeheader()
                        for known_deal in known_deals:
                            writer.writerow(known_deal)
                    discord_messages.append(discord_message)
    my_logger.info("no price drops found for " + friendly_name)
    return discord_messages

# =============================================================================
@to_thread
def load_deal_data_and_start_checking():
    '''
    This function loads the deals to check from craigslist_deals_to_check.csv
    and then checks for new deals and price drops
    '''
    if datetime.datetime.now().hour > 12 or datetime.datetime.now().hour < 2:
        with open('configs/known_deals.csv', 'r') as f:
            known_deals = list(csv.DictReader(f, skipinitialspace=True))
        my_logger.info("loading deals to check from from configs/craigslist_deals_to_check.csv")
        csvfile = "configs/craigslist_deals_to_check.csv"
        with open(csvfile, 'r') as f:
            csv_reader = csv.DictReader(f)
            # Iterate through each row in the CSV file
            discord_messages = []
            for row in csv_reader:
                # Append each row (as a dictionary) to the list
                my_logger.info("check_for_new_deals for " + row['friendly_name'])
                discord_messages.extend(check_for_new_deals(row['friendly_name'], row['url'], known_deals))
                # wait a minute between checking each deal
                time.sleep(60)   
        return discord_messages

# =============================================================================
# This is the main class for the discord bot
# =============================================================================
class DiscordBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(
            command_prefix=commands.when_mentioned_or("!"),
            intents=intents,
            help_command=None,
        )
        """
        This creates custom bot variables so that we can access these variables in cogs more easily.

        For example, The config is available using the following code:
        - self.config # In this class
        - bot.config # In this file
        - self.bot.config # In cogs
        """
    @tasks.loop(minutes=60.0)
    async def status_task(self) -> None:
        """
        Setup the check website task to run every 60 minutes
        """
        discord_messages = await load_deal_data_and_start_checking()
        if discord_messages:
            channel = self.get_channel(CHANNELID)
            my_logger.info(f"Sending messages to channel")
            for discord_message in discord_messages:
                await channel.send(discord_message)

    @status_task.before_loop
    async def before_status_task(self) -> None:
        """
        Before starting the status changing task, we make sure the bot is ready
        """
        await self.wait_until_ready()

    async def setup_hook(self) -> None:
        """
        This will just be executed when the bot starts the first time.
        """
        my_logger.info(f"Logged in as {self.user.name}")
        my_logger.info(f"discord.py API version: {discord.__version__}")
        my_logger.info(f"Python version: {platform.python_version()}")
        my_logger.info(
            f"Running on: {platform.system()} {platform.release()} ({os.name})"
        )
        my_logger.info("-------------------")
        self.status_task.start()


    async def on_command_completion(self, context: Context) -> None:
        """
        The code in this event is executed every time a normal command has been *successfully* executed.

        :param context: The context of the command that has been executed.
        """
        full_command_name = context.command.qualified_name
        split = full_command_name.split(" ")
        executed_command = str(split[0])
        if context.guild is not None:
            my_logger.info(
                f"Executed {executed_command} command in {context.guild.name} (ID: {context.guild.id}) by {context.author} (ID: {context.author.id})"
            )
        else:
            my_logger.info(
                f"Executed {executed_command} command by {context.author} (ID: {context.author.id}) in DMs"
            )

    async def on_command_error(self, context: Context, error) -> None:
        """
        The code in this event is executed every time a normal valid command catches an error.

        :param context: The context of the normal command that failed executing.
        :param error: The error that has been faced.
        """
        if isinstance(error, commands.CommandOnCooldown):
            minutes, seconds = divmod(error.retry_after, 60)
            hours, minutes = divmod(minutes, 60)
            hours = hours % 24
            embed = discord.Embed(
                description=f"**Please slow down** - You can use this command again in {f'{round(hours)} hours' if round(hours) > 0 else ''} {f'{round(minutes)} minutes' if round(minutes) > 0 else ''} {f'{round(seconds)} seconds' if round(seconds) > 0 else ''}.",
                color=0xE02B2B,
            )
            await context.send(embed=embed)
        elif isinstance(error, commands.NotOwner):
            embed = discord.Embed(
                description="You are not the owner of the bot!", color=0xE02B2B
            )
            await context.send(embed=embed)
            if context.guild:
                my_logger.warning(
                    f"{context.author} (ID: {context.author.id}) tried to execute an owner only command in the guild {context.guild.name} (ID: {context.guild.id}), but the user is not an owner of the bot."
                )
            else:
                my_logger.warning(
                    f"{context.author} (ID: {context.author.id}) tried to execute an owner only command in the bot's DMs, but the user is not an owner of the bot."
                )
        elif isinstance(error, commands.MissingPermissions):
            embed = discord.Embed(
                description="You are missing the permission(s) `"
                + ", ".join(error.missing_permissions)
                + "` to execute this command!",
                color=0xE02B2B,
            )
            await context.send(embed=embed)
        elif isinstance(error, commands.BotMissingPermissions):
            embed = discord.Embed(
                description="I am missing the permission(s) `"
                + ", ".join(error.missing_permissions)
                + "` to fully perform this command!",
                color=0xE02B2B,
            )
            await context.send(embed=embed)
        elif isinstance(error, commands.MissingRequiredArgument):
            embed = discord.Embed(
                title="Error!",
                # We need to capitalize because the command arguments have no capital letter in the code and they are the first word in the error message.
                description=str(error).capitalize(),
                color=0xE02B2B,
            )
            await context.send(embed=embed)
        else:
            raise error

if __name__ == "__main__":
    load_dotenv()
    DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
    CHANNELID = int(os.getenv('CHANNELID'))
    bot = DiscordBot()
    bot.run(DISCORD_TOKEN)
