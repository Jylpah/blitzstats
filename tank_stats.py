from argparse import ArgumentParser, Namespace, SUPPRESS
from configparser import ConfigParser
from datetime import datetime
from typing import Optional, Iterable, Any, cast
import logging
from asyncio import create_task, gather, Queue, CancelledError, Task, sleep, wait
from aiofiles import open
from os.path import isfile
from math import ceil
from asyncstdlib import enumerate
from alive_progress import alive_bar		# type: ignore
from yappi import profile 					# type: ignore

from backend import Backend, OptAccountsInactive, BSTableType, ACCOUNTS_Q_MAX, MIN_UPDATE_INTERVAL
from models import BSAccount, BSBlitzRelease, StatsTypes
from accounts import create_accountQ, read_account_strs
from releases import get_releases, release_mapper

from pyutils import get_url, get_url_JSON_model, epoch_now, alive_bar_monitor, \
					is_alphanum, JSONExportable, TXTExportable, CSVExportable, export, \
					BucketMapper, IterableQueue, QueueDone, EventCounter, gather_stats
from blitzutils.models import WoTBlitzReplayJSON, Region, WGApiWoTBlitzTankStats, \
								WGtankStat, Tank
from blitzutils.wg import WGApi 

logger = logging.getLogger()
error 	= logger.error
message	= logger.warning
verbose	= logger.info
debug	= logger.debug

# Constants

WORKERS_WGAPI 		: int = 75
WORKERS_IMPORTERS	: int = 1
TANK_STATS_Q_MAX 	: int = 1000

########################################################
# 
# add_args_ functions  
#
########################################################

def add_args_tank_stats(parser: ArgumentParser, config: Optional[ConfigParser] = None) -> bool:
	try:
		debug('starting')		
		tank_stats_parsers = parser.add_subparsers(dest='tank_stats_cmd', 	
												title='tank-stats commands',
												description='valid commands',
												metavar='get | prune | edit | import | export')
		tank_stats_parsers.required = True
		
		fetch_parser = tank_stats_parsers.add_parser('fetch', aliases=['get'], help="tank-stats fetch help")
		if not add_args_tank_stats_fetch(fetch_parser, config=config):
			raise Exception("Failed to define argument parser for: tank-stats fetch")

		edit_parser = tank_stats_parsers.add_parser('edit', help="tank-stats edit help")
		if not add_args_tank_stats_edit(edit_parser, config=config):
			raise Exception("Failed to define argument parser for: tank-stats edit")
		
		prune_parser = tank_stats_parsers.add_parser('prune', help="tank-stats prune help")
		if not add_args_tank_stats_prune(prune_parser, config=config):
			raise Exception("Failed to define argument parser for: tank-stats prune")

		import_parser = tank_stats_parsers.add_parser('import', help="tank-stats import help")
		if not add_args_tank_stats_import(import_parser, config=config):
			raise Exception("Failed to define argument parser for: tank-stats import")

		export_parser = tank_stats_parsers.add_parser('export', help="tank-stats export help")
		if not add_args_tank_stats_export(export_parser, config=config):
			raise Exception("Failed to define argument parser for: tank-stats export")
		debug('Finished')	
		return True
	except Exception as err:
		error(f'{err}')
	return False


def add_args_tank_stats_fetch(parser: ArgumentParser, config: Optional[ConfigParser] = None) -> bool:
	try:
		debug('starting')
		WG_RATE_LIMIT 	: float = 10
		WORKERS_WGAPI 	: int 	= 10
		WG_APP_ID		: str 	= WGApi.DEFAULT_WG_APP_ID
		
		if config is not None and 'WG' in config.sections():
			configWG 		= config['WG']
			WG_RATE_LIMIT	= configWG.getfloat('rate_limit', WG_RATE_LIMIT)
			WORKERS_WGAPI	= configWG.getint('api_workers', WORKERS_WGAPI)			
			WG_APP_ID		= configWG.get('wg_app_id', WGApi.DEFAULT_WG_APP_ID)

		parser.add_argument('--threads', type=int, default=WORKERS_WGAPI, help='Set number of asynchronous threads')
		parser.add_argument('--wg-app-id', type=str, default=WG_APP_ID, help='Set WG APP ID')
		parser.add_argument('--rate-limit', type=float, default=WG_RATE_LIMIT, metavar='RATE_LIMIT',
							help='Rate limit for WG API')
		parser.add_argument('--region', type=str, nargs='*', choices=[ r.value for r in Region.API_regions() ], 
							default=[ r.value for r in Region.API_regions() ], 
							help='Filter by region (default: eu + com + asia + ru)')
		parser.add_argument('--force', action='store_true', default=False, 
							help='Fetch stats for all accounts')
		parser.add_argument('--sample', type=float, default=0, metavar='SAMPLE',
							help='Fetch tank stats for SAMPLE of accounts. If 0 < SAMPLE < 1, SAMPLE defines a %% of users')
		parser.add_argument('--cache_valid', type=int, default=None, metavar='DAYS',
							help='Fetch stats only for accounts with stats older than DAYS')		
		parser.add_argument('--distributed', '--dist',type=str, dest='distributed', metavar='I:N', 
							default=None, help='Distributed fetching for accounts: id %% N == I')
		parser.add_argument('--check-disabled',  dest='disabled', action='store_true', default=False, 
							help='Check disabled accounts')
		parser.add_argument('--inactive', type=str, choices=[ o.value for o in OptAccountsInactive ], 
								default=OptAccountsInactive.default().value, help='Include inactive accounts')
		parser.add_argument('--accounts', type=int, default=[], nargs='*', metavar='ACCOUNT_ID [ACCOUNT_ID1 ...]',
							help='Fetch tank stats for the listed ACCOUNT_ID(s)')
		parser.add_argument('--file',type=str, metavar='FILENAME', default=None, 
							help='Read account_ids from FILENAME one account_id per line')
		parser.add_argument('--last', action='store_true', default=False, help=SUPPRESS)

		return True
	except Exception as err:
		error(f'{err}')
	return False


def add_args_tank_stats_edit(parser: ArgumentParser, config: Optional[ConfigParser] = None) -> bool:
	debug('starting')
	try:
		edit_parsers = parser.add_subparsers(dest='tank_stats_edit_cmd', 	
												title='tank-stats edit commands',
												description='valid commands',
												metavar='remap-release')
		edit_parsers.required = True
		
		remap_parser = edit_parsers.add_parser('remap-release', help="tank-stats edit remap-release help")
		if not add_args_tank_stats_edit_remap_release(remap_parser, config=config):
			raise Exception("Failed to define argument parser for: tank-stats edit remap-release")		

		return True
	except Exception as err:
		error(f'{err}')
	return False


def add_args_tank_stats_edit_common(parser: ArgumentParser, config: Optional[ConfigParser] = None) -> bool:
	"""Adds common arguments to edit subparser. Required for meaningful helps and usability"""
	debug('starting')
	parser.add_argument('--commit', action='store_true', default=False, 
							help='Do changes instead of just showing what would be changed')
	parser.add_argument('--sample', type=float, default=0, 
						help='Sample size. 0 < SAMPLE < 1 : %% of stats, 1<=SAMPLE : Absolute number')
	# filters
	parser.add_argument('--region', type=str, nargs='*', 
							choices=[ r.value for r in Region.has_stats() ], 
							default=[ r.value for r in Region.has_stats() ], 
							help='Filter by region (default is API = eu + com + asia)')
	parser.add_argument('--release', type=str, metavar='RELEASE', default=None, 
							help='Apply edits to a RELEASE')
	parser.add_argument('--since', type=str, metavar='LAUNCH_DATE', default=None, nargs='?',
						help='Import release launched after LAUNCH_DATE. By default, imports all releases.')
	parser.add_argument('--accounts', type=str, default=[], nargs='*', metavar='ACCOUNT_ID [ACCOUNT_ID1 ...]',
							help="Edit tank stats for the listed ACCOUNT_ID(s). ACCOUNT_ID format 'account_id:region' or 'account_id'")
	parser.add_argument('--tank_ids', type=int, default=None, nargs='*', metavar='TANK_ID [TANK_ID1 ...]',
							help="Edit tank stats for the listed TANK_ID(S).")
	return True


def add_args_tank_stats_edit_remap_release(parser: ArgumentParser, config: Optional[ConfigParser] = None) -> bool:
	debug('starting')
	return add_args_tank_stats_edit_common(parser, config)


def add_args_tank_stats_prune(parser: ArgumentParser, config: Optional[ConfigParser] = None) -> bool:
	debug('starting')
	return True


def add_args_tank_stats_import(parser: ArgumentParser, config: Optional[ConfigParser] = None) -> bool:
	"""Add argument parser for tank-stats import"""
	try:
		debug('starting')
		
		import_parsers = parser.add_subparsers(dest='import_backend', 	
														title='tank-stats import backend',
														description='valid backends', 
														metavar=', '.join(Backend.list_available()))
		import_parsers.required = True

		for backend in Backend.get_registered():
			import_parser =  import_parsers.add_parser(backend.driver, help=f'tank-stats import {backend.driver} help')
			if not backend.add_args_import(import_parser, config=config):
				raise Exception(f'Failed to define argument parser for: tank-stats import {backend.driver}')
		parser.add_argument('--threads', type=int, default=WORKERS_IMPORTERS, help='Set number of asynchronous threads')
		parser.add_argument('--import-type', metavar='IMPORT-TYPE', type=str, 
							default='WGtankStat', choices=['WGtankStat'], 
							help='Data format to import. Default is blitz-stats native format.')
		parser.add_argument('--sample', type=float, default=0, 
							help='Sample size. 0 < SAMPLE < 1 : %% of stats, 1<=SAMPLE : Absolute number')
		parser.add_argument('--force', action='store_true', default=False, 
							help='Overwrite existing stats when importing')
		parser.add_argument('--no-release-map', action='store_true', default=False, 
							help='Do not map releases when importing')
		parser.add_argument('--last', action='store_true', default=False, help=SUPPRESS)

		return True
	except Exception as err:
		error(f'{err}')
	return False


def add_args_tank_stats_export(parser: ArgumentParser, config: Optional[ConfigParser] = None) -> bool:
	try:
		debug('starting')
		EXPORT_FORMAT 	= 'json'
		EXPORT_FILE 	= 'tank_stats'
		EXPORT_SUPPORTED_FORMATS : list[str] = ['json']    # , 'csv'

		if config is not None and 'TANK_STATS' in config.sections():
			configTS 	= config['TANK_STATS']
			EXPORT_FORMAT	= configTS.get('export_format', EXPORT_FORMAT)
			EXPORT_FILE		= configTS.get('export_file', EXPORT_FILE )

		parser.add_argument('format', type=str, nargs='?', choices=EXPORT_SUPPORTED_FORMATS, 
		 					 default=EXPORT_FORMAT, help='Export file format')
		parser.add_argument('filename', metavar='FILE', type=str, nargs='?', default=EXPORT_FILE, 
							help='File to export tank-stats to. Use \'-\' for STDIN')
		parser.add_argument('--append', action='store_true', default=False, help='Append to file(s)')
		parser.add_argument('--force', action='store_true', default=False, 
								help='Overwrite existing file(s) when exporting')
		# parser.add_argument('--disabled', action='store_true', default=False, help='Disabled accounts')
		# parser.add_argument('--inactive', type=str, choices=[ o.value for o in OptAccountsInactive ], 
								# default=OptAccountsInactive.no.value, help='Include inactive accounts')
		parser.add_argument('--region', type=str, nargs='*', choices=[ r.value for r in Region.API_regions() ], 
								default=[ r.value for r in Region.API_regions() ], 
								help='Filter by region (default is API = eu + com + asia)')
		parser.add_argument('--by-region', action='store_true', default=False, help='Export tank-stats by region')
		parser.add_argument('--accounts', type=str, default=[], nargs='*', metavar='ACCOUNT_ID [ACCOUNT_ID1 ...]',
								help="Exports tank stats for the listed ACCOUNT_ID(s). \
									ACCOUNT_ID format 'account_id:region' or 'account_id'")
		parser.add_argument('--tanks', type=int, default=[], nargs='*', metavar='TANK_ID [TANK_ID1 ...]',
								help="Export tank stats for the listed TANK_ID(s)")
		parser.add_argument('--release', type=str, metavar='RELEASE', default=None, 
							help='Export stats for a RELEASE')
		parser.add_argument('--sample', type=float, default=0, 
								help='Sample size. 0 < SAMPLE < 1 : %% of stats, 1<=SAMPLE : Absolute number')
		
		return True	
	except Exception as err:
		error(f'{err}')
	return False


###########################################
# 
# cmd_ functions  
#
###########################################

async def cmd_tank_stats(db: Backend, args : Namespace) -> bool:
	try:
		debug('starting')
		if args.tank_stats_cmd == 'fetch':
			return await cmd_tank_stats_fetch(db, args)

		elif args.tank_stats_cmd == 'edit':
			return await cmd_tank_stats_edit(db, args)

		elif args.tank_stats_cmd == 'export':
			return await cmd_tank_stats_export(db, args)

		elif args.tank_stats_cmd == 'import':
			return await cmd_tank_stats_import(db, args)
			
	except Exception as err:
		error(f'{err}')
	return False

########################################################
# 
# cmd_tank_stats_get()
#
########################################################

async def cmd_tank_stats_fetch(db: Backend, args : Namespace) -> bool:
	"""fetch tank stats"""
	assert 'wg_app_id' in args and type(args.wg_app_id) is str, "'wg_app_id' must be set and string"
	assert 'rate_limit' in args and (type(args.rate_limit) is float or \
			type(args.rate_limit) is int), "'rate_limit' must set and a number"	
	assert 'region' in args and type(args.region) is list, "'region' must be set and a list"
	
	debug('starting')
	wg 	: WGApi = WGApi(WG_app_id=args.wg_app_id, rate_limit=args.rate_limit)

	try:
		stats 	 : EventCounter				= EventCounter('tank-stats fetch')	
		regions	 : set[Region]				= { Region(r) for r in args.region }
		accountQ : IterableQueue[BSAccount]	= IterableQueue(maxsize=ACCOUNTS_Q_MAX)
		retryQ 	 : IterableQueue[BSAccount] | None = None
		statsQ	 : Queue[list[WGtankStat]]	= Queue(maxsize=TANK_STATS_Q_MAX)

		inactive : OptAccountsInactive      = OptAccountsInactive.default()
		try: 
			inactive = OptAccountsInactive(args.inactive)
		except ValueError as err:
			assert False, f"Incorrect value for argument 'inactive': {args.inactive}"

		if not args.disabled:
			retryQ = IterableQueue()		# must not use maxsize
		
		tasks : list[Task] = list()
		tasks.append(create_task(fetch_tank_stats_backend_worker(db, statsQ)))

		# Process accountQ
		accounts : int = await db.accounts_count(StatsTypes.tank_stats, regions=regions, 
												inactive=inactive, disabled=args.disabled, 
												sample=args.sample, cache_valid=args.cache_valid)
		
		task_bar : Task = create_task(alive_bar_monitor([accountQ], total=accounts, 
														title="Fetching tank stats"))
		for _ in range(min([args.threads, ceil(accounts/4)])):
			tasks.append(create_task(fetch_tank_stats_api_worker(db, wg_api=wg, accountQ=accountQ, 
																	statsQ=statsQ, retryQ=retryQ, 
																	disabled=args.disabled)))

		stats.merge_child(await create_accountQ(db, args, accountQ, StatsTypes.tank_stats))
		debug(f'AccountQ created. count={accountQ.count}, size={accountQ.qsize()}')
		await accountQ.join()
		task_bar.cancel()

		# Process retryQ
		if retryQ is not None and not retryQ.empty():
			retry_accounts : int = retryQ.qsize()
			debug(f'retryQ: size={retry_accounts} is_finished={retryQ.is_finished}')
			task_bar = create_task(alive_bar_monitor([retryQ], total=retry_accounts, 
													  title="Retrying failed accounts"))
			for _ in range(min([args.threads, ceil(retry_accounts/4)])):
				tasks.append(create_task(fetch_tank_stats_api_worker(db, wg_api=wg,  
																		accountQ=retryQ, 
																		statsQ=statsQ)))
			await retryQ.join()
			task_bar.cancel()
		
		await statsQ.join()

		for task in tasks:
			task.cancel()
		
		for ec in await gather(*tasks, return_exceptions=True):
			if isinstance(ec, EventCounter):
				stats.merge_child(ec)
		message(stats.print(do_print=False, clean=True))
		return True
	except Exception as err:
		error(f'{err}')
	finally:
	
		wg_stats : dict[str, str] | None = wg.print_server_stats()
		if wg_stats is not None and logger.level <= logging.WARNING:
			message('WG API stats:')
			for server in wg_stats:
				message(f'{server.capitalize():7s}: {wg_stats[server]}')
		await wg.close()
	return False


async def fetch_tank_stats_api_worker(db: Backend, wg_api : WGApi,										
										accountQ: IterableQueue[BSAccount], 
										statsQ	: Queue[list[WGtankStat]], 
										retryQ 	: IterableQueue[BSAccount] | None = None, 
										disabled : bool = False) -> EventCounter:
	"""Async worker to fetch tank stats from WG API"""
	debug('starting')
	stats : EventCounter
	if retryQ is None:
		stats = EventCounter('re-try')
	else:
		stats = EventCounter('fetch')
		await retryQ.add_producer()
		
	try:
		while True:
			account : BSAccount = await accountQ.get()
			# if retryQ is None:
			# 	print(f'retryQ: account_id={account.id}')
			try:
				debug(f'account_id: {account.id}')
				stats.log('accounts total')

				if account.region is None:
					raise ValueError(f'account_id={account.id} does not have region set')
				tank_stats : list[WGtankStat] | None = await wg_api.get_tank_stats(account.id, account.region)

				if tank_stats is None:
					
					debug(f'Could not fetch account: {account.id}')
					if retryQ is not None:
						stats.log('accounts to re-try')
						await retryQ.put(account)
					else:
						stats.log('accounts w/o stats')
						account.disabled = True
						await db.account_update(account=account, fields=['disabled'])
						stats.log('accounts disabled')
				else:					
					await statsQ.put(tank_stats)
					stats.log('tank stats fetched', len(tank_stats))
					stats.log('accounts /w stats')
					if disabled:
						account.disabled = False
						await db.account_update(account=account, fields=['disabled'])
						stats.log('accounts enabled')

			except Exception as err:
				stats.log('errors')
				error(f'{err}')
			finally:
				accountQ.task_done()
	except QueueDone:
		debug('accountQ has been processed')
	except CancelledError as err:
		debug(f'Cancelled')	
	except Exception as err:
		error(f'{err}')
	finally:
		if retryQ is not None:
			await retryQ.finish()

	stats.log('accounts total', - stats.get_value('accounts to re-try'))  # remove re-tried accounts from total
	return stats


async def fetch_tank_stats_backend_worker(db: Backend, statsQ: Queue[list[WGtankStat]]) -> EventCounter:
	"""Async worker to add tank stats to backend. Assumes batch is for the same account"""
	debug('starting')
	stats 		: EventCounter = EventCounter(f'db: {db.driver}')
	added 		: int
	not_added 	: int
	account 	: BSAccount | None
	account_id	: int
	last_battle_time : int

	try:
		releases : BucketMapper[BSBlitzRelease] = await release_mapper(db)
		while True:
			added 			= 0
			not_added 		= 0
			last_battle_time = -1
			account			= None
			tank_stats : list[WGtankStat] = await statsQ.get()
			
			try:
				if len(tank_stats) > 0:
					debug(f'Read {len(tank_stats)} from queue')
					last_battle_time = max( [ ts.last_battle_time for ts in tank_stats] )
					for ts in tank_stats:
						rel : BSBlitzRelease | None = releases.get(ts.last_battle_time)
						if rel is not None:
							ts.release = rel.release
					added, not_added = await db.tank_stats_insert(tank_stats)	
					account_id = tank_stats[0].account_id
					if (account := await db.account_get(account_id=account_id)) is None:
						account = BSAccount(id=account_id)
					account.last_battle_time = last_battle_time
					account.stats_updated(StatsTypes.tank_stats)
					if added > 0:
						stats.log('accounts /w new stats')
						if account.inactive:
							stats.log('accounts marked active')
						account.inactive = False
					else:
						stats.log('accounts w/o new stats')
						if account.is_inactive(StatsTypes.tank_stats): 
							if not account.inactive:
								stats.log('accounts marked inactive')
							account.inactive = True
						
					await db.account_replace(account=account, upsert=True)
			except Exception as err:
				error(f'{err}')
			finally:
				stats.log('tank stats added', added)
				stats.log('old tank stats found', not_added)
				debug(f'{added} tank stats added, {not_added} old tank stats found')				
				statsQ.task_done()
	except CancelledError as err:
		debug(f'Cancelled')	
	except Exception as err:
		error(f'{err}')
	return stats


########################################################
# 
# cmd_tank_stats_edit()
#
########################################################

async def cmd_tank_stats_edit(db: Backend, args : Namespace) -> bool:
	try:
		debug('starting')
		release 	: BSBlitzRelease | None = None
		if args.release is not None:
			release = BSBlitzRelease(release=args.release)
		stats 		: EventCounter 			= EventCounter('tank-stats edit')
		regions		: set[Region] 			= { Region(r) for r in args.region }
		since		: datetime | None 		= None
		if args.since is not None:
			since = datetime.fromisoformat(args.since)
		accounts 	: list[BSAccount] | None= read_account_strs(args.accounts)
		sample 		: float 				= args.sample
		commit 		: bool  				= args.commit

		tank_statQ : Queue[WGtankStat] 		= Queue(maxsize=TANK_STATS_Q_MAX)
		edit_task : Task

		N : int = await db.tank_stats_count(release=release, regions=regions, 
											accounts=accounts,since=since, sample=sample)
		if args.tank_stats_edit_cmd == 'remap-release':
			edit_task = create_task(cmd_tank_stats_edit_rel_remap(db, tank_statQ, commit, N))
		else:
			raise NotImplementedError		
		
		stats.merge_child(await db.tank_stats_get_worker(tank_statQ, release=release, 
														regions=regions, accounts=accounts,
														since=since, sample=sample))
		await tank_statQ.join()
		edit_task.cancel()

		res : EventCounter | BaseException
		for res in await gather(edit_task):
			if isinstance(res, EventCounter):
				stats.merge_child(res)
			elif type(res) is BaseException:
				error(f'{db.backend}: tank-stats edit remap-release returned error: {res}')
		message(stats.print(do_print=False, clean=True))

	except Exception as err:
		error(f'{err}')
	return False


async def cmd_tank_stats_edit_rel_remap(db: Backend, tank_statQ : Queue[WGtankStat], 
										commit: bool = False, total: int | None = None) -> EventCounter:
	"""Remap tank stat's releases"""
	debug('starting')
	stats : EventCounter = EventCounter('remap releases')
	try:
		release 	: BSBlitzRelease | None
		release_map : BucketMapper[BSBlitzRelease] = await release_mapper(db)
		with alive_bar(total, title="Remapping tank stats' releases ", refresh_secs=1,
						enrich_print=False, disable=not commit) as bar:
			while True:
				ts : WGtankStat = await tank_statQ.get()			
				try:
					release = release_map.get(ts.last_battle_time)
					if release is None:
						error(f'Could not map: {ts}')
						stats.log('errors')
						continue
					if ts.release != release.release:
						if commit:
							ts.release = release.release
							debug(f'Remapping {ts.release} to {release.release}: {ts}')
							if await db.tank_stat_update(ts, fields=['release']):
								debug(f'remapped release for {ts}')
								stats.log('updated')
							else:
								debug(f'failed to remap release for {ts}')
								stats.log('failed to update')
						else:
							message(f'Would update release {ts.release} to {release.release} for {ts}')
					else:
						debug(f'No need to remap: {ts}')
						stats.log('no need')			
				except Exception as err:
					error(f'could not remap {ts}: {err}')
				finally:
					tank_statQ.task_done()
					bar()
	except CancelledError as err:
		debug(f'Cancelled')
	except Exception as err:
		error(f'{err}')
	return stats


########################################################
# 
# cmd_tank_stats_export()
#
########################################################

async def cmd_tank_stats_export(db: Backend, args : Namespace) -> bool:
	try:
		debug('starting')		
		assert type(args.sample) in [int, float], 'param "sample" has to be a number'
		
		stats 		: EventCounter 			= EventCounter('tank-stats export')
		regions		: set[Region] 			= { Region(r) for r in args.region }
		filename	: str					= args.filename
		force		: bool 					= args.force
		export_stdout : bool 				= filename == '-'
		sample 		: float = args.sample
		accounts 	: list[BSAccount] | None = read_account_strs(args.accounts)
		tanks 		: list[Tank] | None 	= read_args_tanks(args.tanks)
		release 	: BSBlitzRelease | None = None
		if args.release is not None:
			release = (await get_releases(db, [args.release]))[0]

		tank_statQs 		: dict[str, IterableQueue[WGtankStat]] = dict()
		backend_worker 		: Task 
		export_workers 		: list[Task] = list()
		
		total : int = await db.tank_stats_count(regions=regions, sample=sample, 
												accounts=accounts, tanks=tanks, 
												release=release)

		tank_statQs['all'] = IterableQueue(maxsize=ACCOUNTS_Q_MAX, count_items=not args.by_region)
		# fetch tank_stats for all the regios
		backend_worker = create_task(db.tank_stats_get_worker(tank_statQs['all'], 
																		regions=regions, sample=sample, 
																		accounts=accounts, tanks=tanks, 
																		release=release))
		if args.by_region:
			for region in regions:
				tank_statQs[region.name] = IterableQueue(maxsize=ACCOUNTS_Q_MAX)											
				export_workers.append(create_task(export(Q=cast(Queue[CSVExportable] | Queue[TXTExportable] | Queue[JSONExportable], 
																tank_statQs[region.name]), 
														format=args.format, 
														filename=f'{filename}.{region.name}', 
														force=force, 
														append=args.append)))
			# split by region
			export_workers.append(create_task(split_tank_statQ_by_region(Q_all=tank_statQs['all'], 
									regionQs=cast(dict[str, Queue[WGtankStat]], tank_statQs))))
		else:
			if filename != '-':
				filename += '.all'
			export_workers.append(create_task(export(Q=cast(Queue[CSVExportable] | Queue[TXTExportable] | Queue[JSONExportable], 
															tank_statQs['all']), 
											format=args.format, 
											filename=filename, 
											force=force, 
											append=args.append)))
		bar : Task | None = None
		if not export_stdout:
			bar = create_task(alive_bar_monitor(list(tank_statQs.values()), 'Exporting tank_stats', 
												total=total, enrich_print=False, refresh_secs=1))		
			
		await wait( [backend_worker] )
		for queue in tank_statQs.values():
			await queue.join() 
		if bar is not None:
			bar.cancel()
		for res in await gather(backend_worker):
			if isinstance(res, EventCounter):
				stats.merge_child(res)
			elif type(res) is BaseException:
				error(f'{db.driver}: tank_stats_get_worker() returned error: {res}')
		for worker in export_workers:
			worker.cancel()
		for res in await gather(*export_workers):
			if isinstance(res, EventCounter):
				stats.merge_child(res)
			elif type(res) is BaseException:
				error(f'export(format={args.format}) returned error: {res}')
		if not export_stdout:
			message(stats.print(do_print=False, clean=True))

	except Exception as err:
		error(f'{err}')
	return False




########################################################
# 
# cmd_tank_stats_import()
#
########################################################


async def cmd_tank_stats_import(db: Backend, args : Namespace) -> bool:
	"""Import tank stats from other backend"""	
	try:
		debug('starting')
		assert is_alphanum(args.import_type), f'invalid --import-type: {args.import_type}'
		stats 		: EventCounter 				= EventCounter('tank-stats import')
		tank_statsQ	: Queue[list[WGtankStat]]	= Queue(TANK_STATS_Q_MAX)
		rel_mapQ	: Queue[WGtankStat]			= Queue()
		config 		: ConfigParser | None 		= None
		import_type 	: str 					= args.import_type
		import_backend 	: str 					= args.import_backend
		import_table	: str | None			= args.import_table
		map_releases	: bool 					= not args.no_release_map
		tank_stats_type: type[WGtankStat] 		= globals()[import_type]
		
		WORKERS 	 : int 						= args.threads

		release_map : BucketMapper[BSBlitzRelease] = await release_mapper(db)

		workers : list[Task] = list()
		for _ in range(WORKERS):
			workers.append(create_task(db.tank_stats_insert_worker(tank_statsQ=tank_statsQ, 
																	force=args.force)))
		workers.append(create_task(tank_stats_map_releases_worker(release_map, inputQ=rel_mapQ, 
																outputQ=tank_statsQ, 
																map_releases=map_releases)))
		

		if args.import_config is not None and isfile(args.import_config):
			debug(f'Reading config from {args.config}')
			config = ConfigParser()
			config.read(args.config)

		kwargs : dict[str, Any] = Backend.read_args(args=args, backend=import_backend)
		if (import_db:= Backend.create(args.import_backend, 
										config=config, copy_from=db, **kwargs)) is not None:
			if import_table is not None:
				import_db.set_table(BSTableType.TankStats, import_table)
			elif db == import_db and db.table_tank_stats == import_db.table_tank_stats:
				raise ValueError('Cannot import from itself')
		else:
			raise ValueError(f'Could not init {args.import_backend} to import tank stats from')

		
		assert issubclass(tank_stats_type, WGtankStat), "--import-type has to be subclass of blitzutils.models.WGtankStat" 
		import_db.set_model(import_db.table_tank_stats, tank_stats_type)

		message('Counting tank stats to import ...')
		N : int = await db.tank_stats_count(sample=args.sample)
		
		with alive_bar(N, title="Importing tank stats ", enrich_print=False, refresh_secs=1) as bar:
			async for tank_stat in import_db.tank_stats_export(data_type=tank_stats_type, sample=args.sample):
				await rel_mapQ.put(tank_stat)
				bar()

		await tank_statsQ.join()
		stats = await gather_stats(workers, stats)
		message(stats.print(do_print=False, clean=True))
		return True
	except Exception as err:
		error(f'{err}')	
	return False


async def tank_stats_map_releases_worker(release_map: BucketMapper[BSBlitzRelease], inputQ: Queue[WGtankStat], 
									outputQ: Queue[list[WGtankStat]], 
									map_releases: bool = True) -> EventCounter:
	"""Map tank stats to releases and pack those to list[WGtankStat] queue.
		map_all is None means no release mapping is done"""
	stats = EventCounter('Release mapper')
	IMPORT_BATCH: int = 500
	i 			: int = 0
	release 	: BSBlitzRelease | None
	ts_list 	: list[WGtankStat] = list()

	try:
		debug('starting')
		while True:
			tank_stat = await inputQ.get()
			try:
				if map_releases:
					if tank_stat.release is None:
						if (release := release_map.get(tank_stat.last_battle_time)) is not None:
							tank_stat.release = release.release
							stats.log('mapped')
						else:
							stats.log('errors')
					else:
						stats.log('not mapped')

				if i < IMPORT_BATCH:
					ts_list.append(tank_stat)
					i += 1
				else:
					await outputQ.put(ts_list)
					i = 0
					ts_list = list()
					stats.log('read', IMPORT_BATCH)
			except Exception as err:
				error(f'Processing input queue: {err}')
			finally: 
				inputQ.task_done()
	except CancelledError:
		debug('Cancelled')
	except Exception as err:
		error(f'{err}')	
	return stats



async def split_tank_statQ_by_region(Q_all, regionQs : dict[str, Queue[WGtankStat]], 
								progress: bool = False, 
								bar_title: str = 'Splitting tank stats queue') -> EventCounter:
	debug('starting')
	stats : EventCounter = EventCounter('tank stats')
	try:
		with alive_bar(Q_all.qsize(), title=bar_title, manual=True, refresh_secs=1,
						enrich_print=False, disable=not progress) as bar:
			while True:
				tank_stat : WGtankStat = await Q_all.get()
				try:
					if tank_stat.region is None:
						raise ValueError(f'account ({tank_stat.id}) does not have region defined')
					if tank_stat.region.name in regionQs: 
						await regionQs[tank_stat.region.name].put(tank_stat)
						stats.log(tank_stat.region.name)
					else:
						stats.log(f'excluded region: {tank_stat.region.name}')
				except CancelledError:
					raise CancelledError from None
				except Exception as err:
					stats.log('errors')
					error(f'{err}')
				finally:
					stats.log('total')
					Q_all.task_done()
					bar()
					if progress and Q_all.qsize() == 0:
						break

	except CancelledError as err:
		debug(f'Cancelled')
	except Exception as err:
		error(f'{err}')
	return stats


def read_args_tanks(tank_ids : list[int]) -> list[Tank] | None:
	tanks : list[Tank] = list() 
	for id in tank_ids:
		tanks.append(Tank(tank_id=id))
	if len(tanks) == 0:
		return None
	return tanks