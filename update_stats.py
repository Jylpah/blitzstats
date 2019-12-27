#!/usr/bin/python3.7

# Script fetch Blitz player stats and tank stats

import sys, argparse, json, os, inspect, pprint, aiohttp, asyncio, aiofiles, aioconsole, re, logging, time, xmltodict, collections, pymongo
import motor.motor_asyncio, ssl, configparser, random
import blitzutils as bu
from blitzutils import BlitzStars
from blitzutils import WG

logging.getLogger("asyncio").setLevel(logging.DEBUG)

N_WORKERS = 10
MAX_RETRIES = 3
CACHE_VALID = 5   # 5 days
MAX_UPDATE_INTERVAL = 365*24*3600 # 1 year
SLEEP = 0.5
WG_APP_ID = 'cd770f38988839d7ab858d1cbe54bdd0'

FILE_CONFIG = 'blitzstats.ini'
FILE_ACTIVE_PLAYERS='activeinlast30days.json'

DB_C_ACCOUNTS   		= 'WG_Accounts'
DB_C_PLAYER_STATS		= 'WG_PlayerStats'
DB_C_TANK_STATS     	= 'WG_TankStats'
DB_C_BS_PLAYER_STATS   	= 'BS_PlayerStats'
DB_C_BS_TANK_STATS     	= 'BS_PlayerTankStats'
DB_C_TANKS     			= 'Tankopedia'
DB_C_ERROR_LOG			= 'ErrorLog'
DB_C_UPDATE_LOG			= 'UpdateLog'

UPDATE_FIELD = { 'tank_stats'		: 'updated_WGtankstats',
				'player_stats'		: 'updated_WGplayerstats',
				'player_stats_BS' 	: 'updated_BSplayerstats',
				'tank_stats_BS'		: 'updated_BStankstats'
				}
bs = None
wg = None
stats_added = 0

## main() -------------------------------------------------------------


async def main(argv):
	global bs, wg
	# set the directory for the script
	os.chdir(os.path.dirname(sys.argv[0]))

	parser = argparse.ArgumentParser(description='Analyze Blitz replay JSONs from WoTinspector.com')
	parser.add_argument('--mode', default='help', nargs='+', choices=list(UPDATE_FIELD.keys()) + [ 'tankopedia' ], help='Choose what to update')
	parser.add_argument('--file', default=None, help='JSON file to read')
	parser.add_argument('--force', action='store_true', default=False, help='Force refreshing the active player list')
	parser.add_argument('--workers', type=int, default=N_WORKERS, help='Number of asynchronous workers')
	parser.add_argument('--cache-valid', type=int, dest='cache_valid', default=CACHE_VALID, help='Do not update stats newer than N Days')
	parser.add_argument('--player-src', dest='player_src', default='db', choices=[ 'db', 'blitzstars' ], help='Do NOT use DB for active players')
	parser.add_argument('--sample', type=int, default=0, help='Sample size of accounts to update')
	parser.add_argument('--run-error-log', dest='run_error_log', action='store_true', default=False, help='Re-try previously failed requests')
	arggroup = parser.add_mutually_exclusive_group()
	arggroup.add_argument('-d', '--debug', 		action='store_true', default=False, help='Debug mode')
	arggroup.add_argument('-v', '--verbose', 	action='store_true', default=False, help='Verbose mode')
	arggroup.add_argument('-s', '--silent', 	action='store_true', default=False, help='Silent mode')
	
	args = parser.parse_args()
	args.cache_valid = args.cache_valid*24*3600  # from days to secs	
	bu.set_log_level(args.silent, args.verbose, args.debug)
	bu.set_progress_step(1000)
		
	try:
		bs = BlitzStars()
		wg = WG(WG_APP_ID)

		## Read config
		config 	= configparser.ConfigParser()
		config.read(FILE_CONFIG)
		configDB 	= config['DATABASE']
		DB_SERVER 	= configDB.get('db_server', 'localhost')
		DB_PORT 	= configDB.getint('db_port', 27017)
		DB_SSL 		= configDB.getboolean('db_ssl', False)
		DB_CERT_REQ = configDB.getint('db_ssl_req', ssl.CERT_NONE)
		DB_AUTH 	= configDB.get('db_auth', 'admin')
		DB_NAME 	= configDB.get('db_name', 'BlitzStats')
		DB_USER 	= configDB.get('db_user', 'mongouser')
		DB_PASSWD 	= configDB.get('db_password', "PASSWORD")
		DB_CERT		= configDB.get('db_ssl_cert_file', None)
		DB_CA		= configDB.get('db_ssl_ca_file', None)
		
		#### Connect to MongoDB
		client = motor.motor_asyncio.AsyncIOMotorClient(DB_SERVER,DB_PORT, authSource=DB_AUTH, username=DB_USER, password=DB_PASSWD, ssl=DB_SSL, ssl_cert_reqs=DB_CERT_REQ, ssl_certfile=DB_CERT, tlsCAFile=DB_CA)

		db = client[DB_NAME]
		bu.debug(str(type(db)))	

		await db[DB_C_BS_PLAYER_STATS].create_index([('account_id', pymongo.ASCENDING), ('last_battle_time', pymongo.DESCENDING) ], background=True)	
		await db[DB_C_BS_TANK_STATS].create_index([('account_id', pymongo.ASCENDING), ('tank_id', pymongo.ASCENDING), ('last_battle_time', pymongo.DESCENDING) ], background=True)	
		await db[DB_C_BS_TANK_STATS].create_index([('tank_id', pymongo.ASCENDING), ('last_battle_time', pymongo.DESCENDING) ], background=True)	
		await db[DB_C_TANK_STATS].create_index([('account_id', pymongo.ASCENDING), ('tank_id', pymongo.ASCENDING), ('last_battle_time', pymongo.DESCENDING) ], background=True)	
		await db[DB_C_TANK_STATS].create_index([('tank_id', pymongo.ASCENDING), ('last_battle_time', pymongo.DESCENDING) ], background=True)	
		await db[DB_C_TANKS].create_index([('tank_id', pymongo.ASCENDING), ('tier', pymongo.DESCENDING) ], background=True)	
		await db[DB_C_TANKS].create_index([ ('name', pymongo.TEXT)], background=True)	
		await db[DB_C_ERROR_LOG].create_index([('account_id', pymongo.ASCENDING), ('time', pymongo.DESCENDING), ('type', pymongo.ASCENDING) ], background=True)	
		
		## get active player list ------------------------------
		active_players = {}
		if 'tankopedia' in args.mode:
			await update_tankopedia(db, args.file, args.force)
		else:
			
			if args.run_error_log:
				for mode in get_stat_modes(args.mode):
					active_players[mode] = await get_prev_errors(db, mode)
			elif args.player_src == 'blitzstars':
				bu.debug('src BS')
				tmp_players = await get_active_players_BS(args)
				if args.sample > 0:
					tmp_players = random.sample(tmp_players, args.sample)
				for mode in get_stat_modes(args.mode): 
					active_players[mode] = tmp_players 
			elif args.player_src == 'db':
				bu.debug('src DB')
				for mode in get_stat_modes_WG(args.mode):
					bu.debug('Getting players from DB: ' + mode)
					active_players[mode] = await get_active_players_DB(db, mode, args)
				if (len(get_stat_modes_BS(args.mode)) > 0):
					tmp_players = await get_active_players_BS(args)
					if args.sample > 0:
						tmp_players = random.sample(tmp_players, args.sample)
					for mode in get_stat_modes_BS(args.mode):
						bu.debug('Getting players from BS: ' + mode)
						active_players[mode] = tmp_players
			
		Qcreator_tasks 	= []
		worker_tasks 	= []
		Q = {}

		## set progress bar
		tmp_progress_max = 0
		for mode in set(args.mode) & set(UPDATE_FIELD.keys()):
			tmp_progress_max += len(active_players[mode])
		bu.print_new_line()
		bu.set_progress_bar('Fetching stats', tmp_progress_max)
		for mode in UPDATE_FIELD:
			Q[mode] = asyncio.Queue()
		
		if 'tank_stats' in args.mode:
			mode = 'tank_stats'
			Qcreator_tasks.append(asyncio.create_task(mk_playerQ(Q[mode], active_players[mode])))
			for i in range(args.workers):
				worker_tasks.append(asyncio.create_task(WG_tank_stat_worker( db, Q[mode], i, args )))
				bu.debug('Tank list Task ' + str(i) + ' started')	
				await asyncio.sleep(SLEEP)	
	
		if 'tank_stats_BS' in args.mode:
			mode = 'tank_stats_BS'
			Qcreator_tasks.append(asyncio.create_task(mk_playerQ(Q[mode], active_players[mode])))
			for i in range(args.workers):
				worker_tasks.append(asyncio.create_task(BS_tank_stat_worker(db, Q[mode], i, args )))
				bu.debug('Tank stat Task ' + str(i) + ' started')	
				await asyncio.sleep(SLEEP)

		if 'player_stats' in args.mode:
			mode = 'player_stats'
			bu.error('Fetching WG player stats NOT IMPLEMENTED YET')

		if 'player_stats_BS' in args.mode:
			mode = 'player_stats_BS'
			Qcreator_tasks.append(asyncio.create_task(mk_playerQ(Q[mode], active_players[mode])))
			for i in range(args.workers):
				worker_tasks.append(asyncio.create_task(BS_player_stat_worker(db, Q[mode], i, args )))
				bu.debug('Player stat Task ' + str(i) + ' started')
				await asyncio.sleep(SLEEP)
        		
		## wait queues to finish --------------------------------------
		
		if len(Qcreator_tasks) > 0: 
			bu.debug('Waiting for the work queue makers to finish')
			await asyncio.wait(Qcreator_tasks)

		bu.debug('All active players added to the queue. Waiting for stat workers to finish')
		for mode in UPDATE_FIELD:
			await Q[mode].join()		

		bu.finish_progress_bar()
		
		bu.debug('All work queues empty. Cancelling workers')
		for task in worker_tasks:
			task.cancel()
		bu.debug('Waiting for workers to cancel')
		if len(worker_tasks) > 0:
			await asyncio.gather(*worker_tasks, return_exceptions=True)

		if (args.sample == 0) and (not args.run_error_log):
			# only for full stats
			log_update_time(db, args.mode)
		print_update_stats(args.mode)

	except asyncio.CancelledError as err:
		bu.error('Queue got cancelled while still working.')
	except Exception as err:
		bu.error('Unexpected Exception', err)
	finally:
		bu.print_new_line(True)
		await bs.close()
		await wg.close()

	return None


def get_stat_modes(mode_list: list) -> list:
	"""Return modes of fetching stats (i.e. NOT tankopedia)"""
	return list(set(mode_list) & set(UPDATE_FIELD.keys()))


def get_stat_modes_WG(mode_list: list) -> list:
	"""Return modes of fetching stats from WG API"""
	return list(set(mode_list) & set( [ 'tank_stats', 'player_stats'] ))


def get_stat_modes_BS(mode_list: list) -> list:
	"""Return modes of fetching stats from BlitzStars"""
	return list(set(mode_list) & set( [ 'tank_stats_BS', 'player_stats_BS' ] ))


def log_update_time(db : motor.motor_asyncio.AsyncIOMotorDatabase, mode : list):
	"""Log successfully finished status update"""
	dbc = db[DB_C_UPDATE_LOG]
	try:
		now = bu.NOW()
		for m in mode:
			dbc.insert_one( { 'mode': m, 'updated': now } )
	except Exception as err:
		bu.error('Unexpected Exception', err)
		return False
	return True


async def get_active_players_DB(db : motor.motor_asyncio.AsyncIOMotorDatabase, mode: str, args : argparse.Namespace):
	"""Get list of active accounts from the database"""
	dbc = db[DB_C_ACCOUNTS]
	
	force 		= args.force
	cache_valid = args.cache_valid
	sample 		= args.sample

	players = list()
	
	if sample > 0:
		if force:
			pipeline = [   	{'$sample': {'size' : sample} } ]
		else:
			pipeline = [ 	{'$match': { '$or': [ { UPDATE_FIELD[mode]: None }, { UPDATE_FIELD[mode] : { '$lt': bu.NOW() - cache_valid } } ] }},
                         	{'$sample': {'size' : sample} } ]
		
		cursor = dbc.aggregate(pipeline, allowDiskUse=False)
	else:
		if force:
			cursor = dbc.find()
		else:
			cursor = dbc.find(  { '$or': [ { UPDATE_FIELD[mode]: None }, { UPDATE_FIELD[mode] : { '$lt': bu.NOW() - cache_valid } } ] }, { '_id' : 1} )
	
	i = 0
	tmp_steps = bu.get_progress_step()
	bu.set_progress_step(50000)
	async for player in cursor:
		i += 1
		if bu.print_progress():
			bu.debug('Accounts read from DB: ' + str(i))
		try:
			players.append(player['_id'])
		except Exception as err:
			bu.error('Unexpected error', err)
	
	bu.set_progress_step(tmp_steps)
	bu.debug(str(len(players)) + ' read from the DB')
	return players

async def get_active_players_BS(args : argparse.Namespace):
	"""Get active_players from BlitzStars or local cache file"""
	global bs, wg

	force 		= args.force
	cache_valid = args.cache_valid

	active_players = None
	if force or not (os.path.exists(FILE_ACTIVE_PLAYERS) and os.path.isfile(FILE_ACTIVE_PLAYERS)) or (bu.NOW() - os.path.getmtime(FILE_ACTIVE_PLAYERS) > cache_valid):
		try:
			bu.verbose('Retrieving active players file from BlitzStars.com')
			url = bs.get_url_active_players()
			active_players = await bu.get_url_JSON(bs.session, url)
			await bu.save_JSON(FILE_ACTIVE_PLAYERS, active_players)
		except aiohttp.ClientError as err:
			bu.error("Could not retrieve URL" + url)
			bu.error(exception=err)
		except Exception as err:
			bu.error('Unexpected error', err)
	else:
		async with aiofiles.open(FILE_ACTIVE_PLAYERS, 'rt') as f:
			active_players = json.loads(await f.read())
	return active_players
				

async def get_prev_errors(db : motor.motor_asyncio.AsyncIOMotorDatabase, mode :str):
	"""Get list of acccount_ids of the previous failed requests"""
	dbc = db[DB_C_ERROR_LOG]
	account_ids =  set()
	
	cursor = dbc.find({'type': mode}, { 'account_id': 1, '_id': 0 } )
	async for stat in cursor:
		try:
			account_ids.add(stat['account_id'])
		except Exception as err:
			bu.error('Unexpected error', err)
	return list(account_ids)


async def clear_error_log(db : motor.motor_asyncio.AsyncIOMotorDatabase, account_id, stat_type: str):
	"""Delete ErrorLog entry for account_id, stat_type"""
	dbc = db[DB_C_ERROR_LOG]
	await dbc.delete_many({ 'account_id': account_id, 'type': stat_type })


async def has_fresh_stats(db : motor.motor_asyncio.AsyncIOMotorDatabase, account_id : int, stat_type: str) -> bool:
	"""Check whether the DB has fresh enough stats for the account_id & stat_type"""
	dbc = db[DB_C_ACCOUNTS]
	try:
		update_field = UPDATE_FIELD[stat_type]
		res = await dbc.find_one( { '_id' : account_id })
		if res == None:
			return False
		if (update_field in res) and ('latest_battle_time' in res):
			if (res[update_field] == None) or (res['latest_battle_time'] == None) or (res['latest_battle_time'] > bu.NOW()):
				return False
			elif (bu.NOW() - res[update_field])  > min(MAX_UPDATE_INTERVAL, (res[update_field] - res['latest_battle_time'])/2):
				return False
			
			return True
		else:
			return False
	except Exception as err:
		bu.error('Unexpected error', err)
		return False


def print_update_stats(mode: list):
	if len(set(mode) & set(UPDATE_FIELD.keys())) > 0:
		bu.verbose_std('Total ' + str(stats_added) + ' stats updated')
		return True
	else:
		return False


async def update_stats_update_time(db : motor.motor_asyncio.AsyncIOMotorDatabase, account_id, field, last_battle_time = None) -> bool:
	dbc = db[DB_C_ACCOUNTS]
	try:
		await dbc.update_one( { '_id' : account_id }, { '$set': { 'last_battle_time': last_battle_time, UPDATE_FIELD[field] : bu.NOW() }} )
		return True
	except Exception as err:
		bu.error('Unexpected error', err)
		return False	


async def update_tankopedia( db: motor.motor_asyncio.AsyncIOMotorDatabase, filename: str, force: bool):
	"""Update tankopedia in the database"""
	dbc = db[DB_C_TANKS]
	if filename != None:
		async with aiofiles.open(filename, 'rt', encoding="utf8") as fp:
			tanks = json.loads(await fp.read())
			inserted = 0
			updated = 0
			for tank_id in tanks['data']:
				try:
					tank = tanks['data'][tank_id]
					tank['_id'] = int(tank_id)
					if not all(field in tank for field in ['tank_id', 'name','nation', 'tier','type' ,'is_premium']):
						bu.error('Missing fields in: ' + str(tank))
						continue
					if force:
						await dbc.replace_one( { 'tank_id' : int(tank_id) } , tank, upsert=force)
						updated += 1
					else:
						await dbc.insert_one(tank)
						inserted += 1
				except pymongo.errors.DuplicateKeyError:
					pass
				except Exception as err:
					bu.error('Unexpected error', err)
			bu.verbose_std('Added ' + str(inserted) + ' tanks, updated ' + str(updated) + ' tanks')
			return True			
	else:
		bu.error('--file argument not set')
	return False


async def mk_playerQ(queue : asyncio.Queue, account_ids : list):
	"""Create queue of replays to post"""
	for account_id in account_ids:
		bu.debug('Adding account_id: ' + str(account_id) + ' to the queue')
		await queue.put(account_id)

	return None


async def del_account_id(db: motor.motor_asyncio.AsyncIOMotorDatabase, account_id: int):
	"""Remove account_id from the DB"""
	dbc = db[DB_C_ACCOUNTS]
	try: 
		await dbc.delete_one({ '_id': account_id } )		
	except Exception as err:
		bu.error('Unexpected error', err)
	finally:
		bu.debug('Removed account_id: ' + str(account_id))
	return None


async def BS_player_stat_worker(db : motor.motor_asyncio.AsyncIOMotorDatabase, playerQ : asyncio.Queue, worker_id : int, args : argparse.Namespace):
	"""Async Worker to process the player queue for BlitzStars.com Player stats"""
	dbc = db[DB_C_BS_PLAYER_STATS]
	field = 'player_stats_BS'

	clr_error_log 	= args.run_error_log
	force 			= args.force

	while True:
		account_id = await playerQ.get()
		bu.print_progress()
		try:
			
			url = None
			if (not force) and (await has_fresh_stats(db, account_id, field)):
				bu.debug('Fresh-enough stats for account_id=' + str(account_id) + ' exists in the DB', worker_id)
			else:
				stats = await bs.get_player_stats(account_id)
				if stats == None:
					raise bu.StatsNotFound('BlitzStars player stats not found: account_id ' + str(account_id))
				last_battle_time = -1
				for stat in stats:
					last_battle_time = max(last_battle_time, stat['last_battle_time'])
				try: 
					await dbc.insert_many(stats, ordered=False)
				except pymongo.errors.BulkWriteError as err:
					pass
				finally:
					await update_stats_update_time(db, account_id, field, last_battle_time)
					bu.debug('Added stats for account_id=' + str(account_id), worker_id)		
		except bu.StatsNotFound as err:
			bu.debug(str(err))
			await log_error(db, account_id, field, clr_error_log)		
		except Exception as err:
			bu.error('Unexpected error: ' + ((' URL: ' + url) if url!= None else ""), err, worker_id)
			await log_error(db, account_id, field, clr_error_log)
		finally:
			if clr_error_log:
				await clear_error_log(db, account_id, field)
			playerQ.task_done()		
			await asyncio.sleep(SLEEP)
	return None


async def BS_tank_stat_worker(db : motor.motor_asyncio.AsyncIOMotorDatabase, playerQ : asyncio.Queue, worker_id : int, args : argparse.Namespace):
	"""Async Worker to fetch players' tank stats from BlitzStars.com"""
	global stats_added
	
	dbc = db[DB_C_TANK_STATS]   # WG_TankStats
	field = 'tank_stats_BS'

	clr_error_log 	= args.run_error_log
	force 			= args.force

	while True:
		account_id = await playerQ.get()
		bu.print_progress()
		try:
			url = None
			if (not force) and (await has_fresh_stats(db, account_id, field)):
				bu.debug('Fresh-enough stats for account_id=' + str(account_id) + ' exists in the DB', worker_id)
			else:
					
				stats = await bs.get_player_tank_stats(account_id, cache=False)				
				if (stats == None) or (len(stats) == 0):
					bu.debug('Did not receive stats for account_id=' + str(account_id))
				else:
					stats = await bs.tank_stats2WG(stats)  ## Stats conversion
					tank_stats = []
					for tank_stat in stats:
						#bu.debug(str(tank_stat))
						account_id 			= tank_stat['account_id'] 
						tank_id 			= tank_stat['tank_id']
						last_battle_time 	= tank_stat['last_battle_time']
						tank_stat['_id']  	= mk_id(account_id, tank_id, last_battle_time)
						tank_stats.append(tank_stat)
					try: 
						## Add functionality to filter out those stats that aretoo close to existing stats
						res = await dbc.insert_many(tank_stats, ordered=False)
						tmp = len(res.inserted_ids)
						bu.debug(str(tmp) + ' stats added (insert_many() result)', worker_id)
						stats_added += tmp
					except pymongo.errors.BulkWriteError as err:
						tmp = err.details['nInserted']
						bu.debug(str(tmp) + ' stats added', worker_id)
						stats_added += tmp								
					finally:
						await update_stats_update_time(db, account_id, field, last_battle_time)
						bu.debug('Added stats for account_id=' + str(account_id), worker_id)	

		except Exception as err:
			bu.error('Unexpected error: ' + ((' URL: ' + url) if url!= None else ""), err, worker_id)
			await log_error(db, account_id, field, clr_error_log)
		finally:
			if clr_error_log:
				await clear_error_log(db, account_id, field)
			playerQ.task_done()	
			await asyncio.sleep(SLEEP)
	return None


async def WG_tank_stat_worker(db : motor.motor_asyncio.AsyncIOMotorDatabase, playerQ : asyncio.Queue, worker_id : int, args : argparse.Namespace):
	"""Async Worker to process the replay queue: WG player tank stats """
	global stats_added
	
	dbc = db[DB_C_TANK_STATS]
	field = 'tank_stats'

	clr_error_log 	= args.run_error_log
	force 			= args.force
	
	while True:
		account_id = await playerQ.get()
		bu.print_progress()
		try:
			#TBD: Check acccounts, fetch data, update accounts, even if not data fetched. 
			url = None
			if (not force) and (await has_fresh_stats(db, account_id, field)):
				bu.debug(' account_id=' + str(account_id) + ' has Fresh-enough stats in the DB', worker_id)
			else:	
				bu.debug('account_id=' + str(account_id) + ' does not have Fresh-enough stats in the DB', worker_id)
				#url = wg.getUrlPlayerTankList(account_id)	
				#bu.error('[' + str(worker_id) + ']: URL: ' + url)			
				stats = await wg.get_player_tank_stats(account_id, cache=False)
				if stats == None:
					raise bu.StatsNotFound('WG API return NULL stats for ' + str(account_id))
				tank_stats = []
				latest_battle = 0
				for tank_stat in stats:
					tank_id 			= tank_stat['tank_id']
					last_battle_time 	= tank_stat['last_battle_time']
					tank_stat['_id']  	= mk_id(account_id, tank_id, last_battle_time)
					tank_stats.append(tank_stat)

					if (last_battle_time > latest_battle):
						latest_battle = last_battle_time 
				#	RECOMMENDATION TO USE SINGLE INSERTS OVER MANY
				try: 
					res = await dbc.insert_many(tank_stats, ordered=False)
					tmp = len(res.inserted_ids)
					bu.debug(str(tmp) + ' stats added (insert_many() result)', worker_id)
					stats_added += tmp
					#stats_added += len(res.inserted_ids)					
				except pymongo.errors.BulkWriteError as err:
					tmp = err.details['nInserted']
					bu.debug(str(tmp) + ' stats added', worker_id)
					stats_added += tmp										
				finally:
					await update_stats_update_time(db, account_id, field, latest_battle)
					bu.debug('Added stats for account_id=' + str(account_id), worker_id)	
	
		except bu.StatsNotFound as err:
			bu.debug(str(err))
			await log_error(db, account_id, field, clr_error_log)
		except Exception as err:
			bu.error('Unexpected error: ' + ((' URL: ' + url) if url!= None else ""), err, worker_id)
			await log_error(db, account_id, field, clr_error_log)
		finally:
			if clr_error_log:
				await clear_error_log(db, account_id, field)
			playerQ.task_done()	
			await asyncio.sleep(SLEEP)
	return None


async def log_error(db : motor.motor_asyncio.AsyncIOMotorDatabase, account_id: int, stat_type: str, clr_error_log = False):
	dbc = db[DB_C_ERROR_LOG]
	try:
		if clr_error_log:
			await del_account_id(db, account_id)
		else:
			await dbc.insert_one( {'account_id': account_id, 'type': stat_type, 'time': bu.NOW() } )
			#bu.debug('Logging Error: account_id=' + str(account_id) + ' stat_type=' + stat_type)
	except Exception as err:
		bu.error('Unexpected error', err)


def mk_id(account_id: int, tank_id: int, last_battle_time: int) -> str:
	return hex(account_id)[2:].zfill(10) + hex(tank_id)[2:].zfill(6) + hex(last_battle_time)[2:].zfill(8)


### main()
if __name__ == "__main__":
   #asyncio.run(main(sys.argv[1:]), debug=True)
   asyncio.run(main(sys.argv[1:]))
