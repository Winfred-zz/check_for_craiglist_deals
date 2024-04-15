# check_for_craiglist_deals
Checks craigslist postings and alerts on Discord

### To use:

Create a .env file in the root of the project. the .env file should look like this:

    DISCORD_TOKEN=ASDFASDASDA123423DQWEQWEQWEQWE
    CHANNELID=23424523456234234234

You can find guides online on how to create the discord bot and token and find the channel id.

In the configs directory, create a file called "craigslist_deals_to_check.csv"

It should look like this:

    friendly_name,url
    somegenericterm,https://craigslist.org/search/actualsearchurl(justcopyfrombrowser)

It's recommended to run this in a virtual environment (as always)

To install the prerequisites, run:

    pip install -r requirements.txt

