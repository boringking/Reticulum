import os
import RNS
import time
import math
import struct
import threading
import traceback
from time import sleep
import vendor.umsgpack as umsgpack

class Transport:
	# Constants
	BROADCAST    = 0x00;
	TRANSPORT    = 0x01;
	RELAY        = 0x02;
	TUNNEL       = 0x03;
	types        = [BROADCAST, TRANSPORT, RELAY, TUNNEL]

	REACHABILITY_UNREACHABLE = 0x00
	REACHABILITY_DIRECT      = 0x01
	REACHABILITY_TRANSPORT	 = 0x02

	APP_NAME = "rnstransport"

	# TODO: Document the addition of random windows
	# and max local rebroadcasts.
	PATHFINDER_M    = 18		# Max hops
	PATHFINDER_C    = 2.0		# Decay constant
	PATHFINDER_R	= 1			# Retransmit retries
	PATHFINDER_T	= 10		# Retry grace period
	PATHFINDER_RW   = 10		# Random window for announce rebroadcast
	PATHFINDER_E    = 60*15		# Path expiration in seconds

	# TODO: Calculate an optimal number for this in
	# various situations
	LOCAL_REBROADCASTS_MAX = 2	# How many local rebroadcasts of an announce is allowed

	PATH_REQUEST_GRACE  = 0.25       # Grace time before a path announcement is made, allows directly reachable peers to respond first
	PATH_REQUEST_RW     = 2 		 # Path request random window

	LINK_TIMEOUT        = RNS.Link.KEEPALIVE * 2
	REVERSE_TIMEOUT     = 30*60		 # Reverse table entries are removed after max 30 minutes
	DESTINATION_TIMEOUT = 60*60*24*7 # Destination table entries are removed if unused for one week

	interfaces	 	    = []		 # All active interfaces
	destinations        = []		 # All active destinations
	pending_links       = []		 # Links that are being established
	active_links	    = []		 # Links that are active
	packet_hashlist     = []		 # A list of packet hashes for duplicate detection
	receipts		    = []		 # Receipts of all outgoing packets for proof processing

	announce_table      = {}		 # A table for storing announces currently waiting to be retransmitted
	destination_table   = {}		 # A lookup table containing the next hop to a given destination
	reverse_table	    = {}		 # A lookup table for storing packet hashes used to return proofs and replies
	link_table          = {}		 # A lookup table containing hops for links

	jobs_locked = False
	jobs_running = False
	job_interval = 0.250
	receipts_last_checked    = 0.0
	receipts_check_interval  = 1.0
	announces_last_checked   = 0.0
	announces_check_interval = 1.0
	hashlist_maxsize         = 1000000
	tables_last_culled       = 0.0
	tables_cull_interval	 = 5.0

	identity = None

	@staticmethod
	def start():
		if Transport.identity == None:
			transport_identity_path = RNS.Reticulum.configdir+"/transportidentity"
			if os.path.isfile(transport_identity_path):
				Transport.identity = RNS.Identity.from_file(transport_identity_path)				

			if Transport.identity == None:
				RNS.log("No valid Transport Identity on disk, creating...", RNS.LOG_VERBOSE)
				Transport.identity = RNS.Identity()
				Transport.identity.save(transport_identity_path)
			else:
				RNS.log("Loaded Transport Identity from disk", RNS.LOG_VERBOSE)

		packet_hashlist_path = RNS.Reticulum.configdir+"/packet_hashlist"
		if os.path.isfile(packet_hashlist_path):
			try:
				file = open(packet_hashlist_path, "r")
				Transport.packet_hashlist = umsgpack.unpackb(file.read())
				file.close()
			except Exception as e:
				RNS.log("Could not load packet hashlist from disk, the contained exception was: "+str(e), RNS.LOG_ERROR)

		# Create transport-specific destinations
		path_request_destination = RNS.Destination(None, RNS.Destination.IN, RNS.Destination.PLAIN, Transport.APP_NAME, "path", "request")
		path_request_destination.packet_callback(Transport.pathRequestHandler)
		
		thread = threading.Thread(target=Transport.jobloop)
		thread.setDaemon(True)
		thread.start()

		RNS.log("Transport instance "+str(Transport.identity)+" started")

	@staticmethod
	def jobloop():
		while (True):
			Transport.jobs()
			sleep(Transport.job_interval)

	@staticmethod
	def jobs():
		outgoing = []
		Transport.jobs_running = True
		try:
			if not Transport.jobs_locked:
				# Process receipts list for timed-out packets
				if time.time() > Transport.receipts_last_checked+Transport.receipts_check_interval:
					for receipt in Transport.receipts:
						thread = threading.Thread(target=receipt.check_timeout)
						thread.setDaemon(True)
						thread.start()
						if receipt.status != RNS.PacketReceipt.SENT:
							Transport.receipts.remove(receipt)

					Transport.receipts_last_checked = time.time()

				# Process announces needing retransmission
				if time.time() > Transport.announces_last_checked+Transport.announces_check_interval:
					for destination_hash in Transport.announce_table:
						announce_entry = Transport.announce_table[destination_hash]
						if announce_entry[2] > Transport.PATHFINDER_R:
							RNS.log("Dropping announce for "+RNS.prettyhexrep(destination_hash)+", retries exceeded", RNS.LOG_DEBUG)
							Transport.announce_table.pop(destination_hash)
							break
						else:
							if time.time() > announce_entry[1]:
								announce_entry[1] = time.time() + math.pow(Transport.PATHFINDER_C, announce_entry[4]) + Transport.PATHFINDER_T + Transport.PATHFINDER_RW
								announce_entry[2] += 1
								packet = announce_entry[5]
								block_rebroadcasts = announce_entry[7]
								announce_context = RNS.Packet.NONE
								if block_rebroadcasts:
									announce_context = RNS.Packet.PATH_RESPONSE
								announce_data = packet.data
								announce_identity = RNS.Identity.recall(packet.destination_hash)
								announce_destination = RNS.Destination(announce_identity, RNS.Destination.OUT, RNS.Destination.SINGLE, "unknown", "unknown");
								announce_destination.hash = packet.destination_hash
								announce_destination.hexhash = announce_destination.hash.encode("hex_codec")
								new_packet = RNS.Packet(announce_destination, announce_data, RNS.Packet.ANNOUNCE, context = announce_context, header_type = RNS.Packet.HEADER_2, transport_type = Transport.TRANSPORT, transport_id = Transport.identity.hash)
								new_packet.hops = announce_entry[4]
								RNS.log("Rebroadcasting announce for "+RNS.prettyhexrep(announce_destination.hash)+" with hop count "+str(new_packet.hops), RNS.LOG_DEBUG)
								outgoing.append(new_packet)

					Transport.announces_last_checked = time.time()


				# Cull the packet hashlist if it has reached max size
				while (len(Transport.packet_hashlist) > Transport.hashlist_maxsize):
					Transport.packet_hashlist.pop(0)

				if time.time() > Transport.tables_last_culled + Transport.tables_cull_interval:
					# Cull the reverse table according to timeout
					for truncated_packet_hash in Transport.reverse_table:
						reverse_entry = Transport.reverse_table[truncated_packet_hash]
						if time.time() > reverse_entry[2] + Transport.REVERSE_TIMEOUT:
							Transport.reverse_table.pop(truncated_packet_hash)

					# Cull the link table according to timeout
					for link_id in Transport.link_table:
						link_entry = Transport.link_table[link_id]
						if time.time() > link_entry[0] + Transport.LINK_TIMEOUT:
							Transport.link_table.pop(link_id)

					# Cull the destination table in some way
					for destination_hash in Transport.destination_table:
						destination_entry = Transport.destination_table[destination_hash]
						if time.time() > destination_entry[0] + Transport.DESTINATION_TIMEOUT:
							Transport.destination_table.pop(destination_hash)

					Transport.tables_last_culled = time.time()

		except Exception as e:
			RNS.log("An exception occurred while running Transport jobs.", RNS.LOG_ERROR)
			RNS.log("The contained exception was: "+str(e), RNS.LOG_ERROR)
			traceback.print_exc()

		Transport.jobs_running = False

		for packet in outgoing:
			packet.send()

	@staticmethod
	def outbound(packet):
		while (Transport.jobs_running):
			sleep(0.01)

		Transport.jobs_locked = True
		# TODO: This updateHash call might be redundant
		packet.updateHash()
		sent = False

		# Check if we have a known path for the destination
		# in the destination table
		if packet.packet_type != RNS.Packet.ANNOUNCE and packet.destination_hash in Transport.destination_table:
			outbound_interface = Transport.destination_table[packet.destination_hash][5]

			if Transport.destination_table[packet.destination_hash][2] > 1:
				# Insert packet into transport
				new_flags = (RNS.Packet.HEADER_2) << 6 | (Transport.TRANSPORT) << 4 | (packet.flags & 0b00001111)
				new_raw = struct.pack("!B", new_flags)
				new_raw += packet.raw[1:2]
				new_raw += Transport.destination_table[packet.destination_hash][1]
				new_raw += packet.raw[2:]
				RNS.log("Packet was inserted into transport via "+RNS.prettyhexrep(Transport.destination_table[packet.destination_hash][1])+" on: "+str(outbound_interface), RNS.LOG_DEBUG)
				outbound_interface.processOutgoing(new_raw)
				Transport.destination_table[packet.destination_hash][0] = time.time()
				sent = True
			else:
				# Destination is directly reachable, and we know on
				# what interface, so transmit only on that one

				RNS.log("Transmitting "+str(len(packet.raw))+" bytes on: "+str(outbound_interface), RNS.LOG_EXTREME)
				RNS.log("Hash is "+RNS.prettyhexrep(packet.packet_hash), RNS.LOG_EXTREME)
				outbound_interface.processOutgoing(packet.raw)
				sent = True

		else:
			# Broadcast packet on all outgoing interfaces, or relevant
			# interface, if packet is for a link or has an attachede interface
			for interface in Transport.interfaces:
				if interface.OUT:
					should_transmit = True
					if packet.destination.type == RNS.Destination.LINK:
						if packet.destination.status == RNS.Link.CLOSED:
							should_transmit = False
						if interface != packet.destination.attached_interface:
							should_transmit = False
					if packet.attached_interface != None and interface != packet.attached_interface:
						should_transmit = False
							
					if should_transmit:
						RNS.log("Transmitting "+str(len(packet.raw))+" bytes on: "+str(interface), RNS.LOG_EXTREME)
						RNS.log("Hash is "+RNS.prettyhexrep(packet.packet_hash), RNS.LOG_EXTREME)
						interface.processOutgoing(packet.raw)
						sent = True

		if sent:
			packet.sent = True
			packet.sent_at = time.time()

			if (packet.packet_type == RNS.Packet.DATA and packet.destination.type != RNS.Destination.PLAIN):
				packet.receipt = RNS.PacketReceipt(packet)
				Transport.receipts.append(packet.receipt)
			
			Transport.cache(packet)

		Transport.jobs_locked = False
		return sent

	@staticmethod
	def packet_filter(packet):
		# TODO: Think long and hard about this
		if packet.context == RNS.Packet.KEEPALIVE:
			return True
		if packet.context == RNS.Packet.RESOURCE_REQ:
			return True
		if packet.context == RNS.Packet.RESOURCE_PRF:
			return True
		if not packet.packet_hash in Transport.packet_hashlist:
			return True
		else:
			if packet.packet_type == RNS.Packet.ANNOUNCE:
				return True

		RNS.log("Filtered packet with hash "+RNS.prettyhexrep(packet.packet_hash), RNS.LOG_DEBUG)
		return False

	@staticmethod
	def inbound(raw, interface=None):
		while (Transport.jobs_running):
			sleep(0.1)
			
		Transport.jobs_locked = True
		
		packet = RNS.Packet(None, raw)
		packet.unpack()
		packet.receiving_interface = interface
		packet.hops += 1

		RNS.log(str(interface)+" received packet with hash "+RNS.prettyhexrep(packet.packet_hash), RNS.LOG_EXTREME)

		if Transport.packet_filter(packet):
			Transport.packet_hashlist.append(packet.packet_hash)
			Transport.cache(packet)
			
			# General transport handling. Takes care of directing
			# packets according to transport tables and recording
			# entries in reverse and link tables.
			if packet.transport_id != None and packet.packet_type != RNS.Packet.ANNOUNCE:
				if packet.transport_id == Transport.identity.hash:
					RNS.log("Received packet in transport for "+RNS.prettyhexrep(packet.destination_hash)+" with matching transport ID, transporting it...", RNS.LOG_DEBUG)
					if packet.destination_hash in Transport.destination_table:
						next_hop = Transport.destination_table[packet.destination_hash][1]
						remaining_hops = Transport.destination_table[packet.destination_hash][2]
						RNS.log("Next hop to destination is "+RNS.prettyhexrep(next_hop)+" with "+str(remaining_hops)+" hops remaining, transporting it.", RNS.LOG_DEBUG)
						if remaining_hops > 1:
							# Just increase hop count and transmit
							new_raw = packet.raw[0:1]
							new_raw += struct.pack("!B", packet.hops)
							new_raw += next_hop
							new_raw += packet.raw[12:]
						else:
							# Strip transport headers and transmit
							new_flags = (RNS.Packet.HEADER_1) << 6 | (Transport.BROADCAST) << 4 | (packet.flags & 0b00001111)
							new_raw = struct.pack("!B", new_flags)
							new_raw += struct.pack("!B", packet.hops)
							new_raw += packet.raw[12:]

						outbound_interface = Transport.destination_table[packet.destination_hash][5]
						outbound_interface.processOutgoing(new_raw)
						Transport.destination_table[packet.destination_hash][0] = time.time()

						if packet.packet_type == RNS.Packet.LINKREQUEST:
							# Entry format is
							link_entry = [	time.time(),					# 0: Timestamp,
											next_hop,						# 1: Next-hop transport ID
											outbound_interface,				# 2: Next-hop interface
											remaining_hops,					# 3: Remaining hops
											packet.receiving_interface,		# 4: Received on interface
											packet.hops,					# 5: Taken hops
											packet.destination_hash,		# 6: Original destination hash
											False]							# 7: Validated

							Transport.link_table[packet.getTruncatedHash()] = link_entry

						else:
							# Entry format is
							reverse_entry = [	packet.receiving_interface,	# 0: Received on interface
												outbound_interface,			# 1: Outbound interface
												time.time()]				# 2: Timestamp

							Transport.reverse_table[packet.getTruncatedHash()] = reverse_entry

					else:
						# TODO: There should probably be some kind of REJECT
						# mechanism here, to signal to the source that their
						# expected path failed
						RNS.log("Got packet in transport, but no known path to final destination. Dropping packet.", RNS.LOG_DEBUG)
				else:
					pass

			# Link transport handling. Directs packetes according
			# to entries in the link tables
			if packet.packet_type != RNS.Packet.ANNOUNCE and packet.packet_type != RNS.Packet.LINKREQUEST:
				if packet.destination_hash in Transport.link_table:
					link_entry = Transport.link_table[packet.destination_hash]
					# If receiving and outbound interface is
					# the same for this link, direction doesn't
					# matter, and we simply send the packet on.
					outbound_interface = None
					if link_entry[2] == link_entry[4]:
						# But check that taken hops matches one
						# of the expectede values.
						if packet.hops == link_entry[3] or packet.hops == link_entry[5]:
							outbound_interface = link_entry[2]
					else:
						# If interfaces differ, we transmit on
						# the opposite interface of what the
						# packet was received on.
						if packet.receiving_interface == link_entry[2]:
							# Also check that expected hop count matches
							if packet.hops == link_entry[3]:
								outbound_interface = link_entry[4]
						elif packet.receiving_interface == link_entry[4]:
							# Also check that expected hop count matches
							if packet.hops == link_entry[5]:
								outbound_interface = link_entry[2]
						
					if outbound_interface != None:
						new_raw = packet.raw[0:1]
						new_raw += struct.pack("!B", packet.hops)
						new_raw += packet.raw[2:]
						outbound_interface.processOutgoing(new_raw)
						Transport.link_table[packet.destination_hash][0] = time.time()
					else:
						pass


			# Announce handling. Handles logic related to incoming
			# announces, queueing rebroadcasts of these, and removal
			# of queued announce rebroadcasts once handed to the next node.
			if packet.packet_type == RNS.Packet.ANNOUNCE:
				local_destination = next((d for d in Transport.destinations if d.hash == packet.destination_hash), None)
				if local_destination == None and RNS.Identity.validateAnnounce(packet):
					if packet.transport_id != None:
						received_from = packet.transport_id
						
						# Check if this is a next retransmission from
						# another node. If it is, we're removing the
						# announce in question from our pending table
						if packet.destination_hash in Transport.announce_table:
							announce_entry = Transport.announce_table[packet.destination_hash]
							
							if packet.hops-1 == announce_entry[4]:
								RNS.log("Heard a local rebroadcast of announce for "+RNS.prettyhexrep(packet.destination_hash), RNS.LOG_DEBUG)
								announce_entry[6] += 1
								if announce_entry[6] >= Transport.LOCAL_REBROADCASTS_MAX:
									RNS.log("Max local rebroadcasts of announce for "+RNS.prettyhexrep(packet.destination_hash)+" reached, dropping announce from our table", RNS.LOG_DEBUG)
									Transport.announce_table.pop(packet.destination_hash)

							if packet.hops-1 == announce_entry[4]+1 and announce_entry[2] > 0:
								now = time.time()
								if now < announce_entry[1]:
									RNS.log("Rebroadcasted announce for "+RNS.prettyhexrep(packet.destination_hash)+" has been passed on to next node, no further tries needed", RNS.LOG_DEBUG)
									Transport.announce_table.pop(packet.destination_hash)

					else:
						received_from = packet.destination_hash

					# Check if this announce should be inserted into
					# announce and destination tables
					should_add = False

					# First, check that the announce is not for a destination
					# local to this system, and that hops are less than the max
					if (not any(packet.destination_hash == d.hash for d in Transport.destinations) and packet.hops < Transport.PATHFINDER_M+1):
						random_blob = packet.data[RNS.Identity.DERKEYSIZE/8+10:RNS.Identity.DERKEYSIZE/8+20]
						random_blobs = []
						if packet.destination_hash in Transport.destination_table:
							random_blobs = Transport.destination_table[packet.destination_hash][4]

							# If we already have a path to the announced
							# destination, but the hop count is equal or
							# less, we'll update our tables.
							if packet.hops <= Transport.destination_table[packet.destination_hash][2]:
								# Make sure we haven't heard the random
								# blob before, so announces can't be
								# replayed to forge paths.
								# TODO: Check whether this approach works
								# under all circumstances
								if not random_blob in random_blobs:
									should_add = True
								else:
									should_add = False
							else:
								# If an announce arrives with a larger hop
								# count than we already have in the table,
								# ignore it, unless the path is expired
								if (time.time() > Transport.destination_table[packet.destination_hash][3]):
									# We also check that the announce hash is
									# different from ones we've already heard,
									# to avoid loops in the network
									if not random_blob in random_blobs:
										# TODO: Check that this ^ approach actually
										# works under all circumstances
										RNS.log("Replacing destination table entry for "+str(RNS.prettyhexrep(packet.destination_hash))+" with new announce due to expired path", RNS.LOG_DEBUG)
										should_add = True
									else:
										should_add = False
								else:
									should_add = False
						else:
							# If this destination is unknown in our table
							# we should add it
							should_add = True

						if should_add:
							now = time.time()
							retries = 0
							expires = now + Transport.PATHFINDER_E
							local_rebroadcasts = 0
							block_rebroadcasts = False
							random_blobs.append(random_blob)
							retransmit_timeout = now + math.pow(Transport.PATHFINDER_C, packet.hops) + (RNS.rand() * Transport.PATHFINDER_RW)

							if packet.context != RNS.Packet.PATH_RESPONSE:
								Transport.announce_table[packet.destination_hash] = [now, retransmit_timeout, retries, received_from, packet.hops, packet, local_rebroadcasts, block_rebroadcasts]

							Transport.destination_table[packet.destination_hash] = [now, received_from, packet.hops, expires, random_blobs, packet.receiving_interface, packet]
							RNS.log("Path to "+RNS.prettyhexrep(packet.destination_hash)+" is now "+str(packet.hops)+" hops away via "+RNS.prettyhexrep(received_from)+" on "+str(packet.receiving_interface), RNS.LOG_DEBUG)
			
			elif packet.packet_type == RNS.Packet.LINKREQUEST:
				for destination in Transport.destinations:
					if destination.hash == packet.destination_hash and destination.type == packet.destination_type:
						packet.destination = destination
						destination.receive(packet)
			
			elif packet.packet_type == RNS.Packet.DATA:
				if packet.destination_type == RNS.Destination.LINK:
					for link in Transport.active_links:
						if link.link_id == packet.destination_hash:
							packet.link = link
							link.receive(packet)
				else:
					for destination in Transport.destinations:
						if destination.hash == packet.destination_hash and destination.type == packet.destination_type:
							packet.destination = destination
							destination.receive(packet)

							if destination.proof_strategy == RNS.Destination.PROVE_ALL:
								packet.prove()

							elif destination.proof_strategy == RNS.Destination.PROVE_APP:
								if destination.callbacks.proof_requested:
									if destination.callbacks.proof_requested(packet):
										packet.prove()

			elif packet.packet_type == RNS.Packet.PROOF:
				if packet.context == RNS.Packet.LRPROOF:
					# This is a link request proof, check if it
					# needs to be transported

					if packet.destination_hash in Transport.link_table:
						link_entry = Transport.link_table[packet.destination_hash]
						if packet.receiving_interface == link_entry[2]:
							# TODO: Should we validate the LR proof at each transport
							# step before transporting it?
							RNS.log("Link request proof received on correct interface, transporting it via "+str(link_entry[4]), RNS.LOG_DEBUG)
							new_raw = packet.raw[0:1]
							new_raw += struct.pack("!B", packet.hops)
							new_raw += packet.raw[2:]
							Transport.link_table[packet.destination_hash][7] = True
							link_entry[4].processOutgoing(new_raw)
						else:
							RNS.log("Link request proof received on wrong interface, not transporting it.", RNS.LOG_DEBUG)
					else:
						# Check if we can deliver it to a local
						# pending link
						for link in Transport.pending_links:
							if link.link_id == packet.destination_hash:
								link.validateProof(packet)

				elif packet.context == RNS.Packet.RESOURCE_PRF:
					for link in Transport.active_links:
						if link.link_id == packet.destination_hash:
							link.receive(packet)
				else:
					if packet.destination_type == RNS.Destination.LINK:
						for link in Transport.active_links:
							if link.link_id == packet.destination_hash:
								packet.link = link
								# plaintext = link.decrypt(packet.data)
								
					if len(packet.data) == RNS.PacketReceipt.EXPL_LENGTH:
						proof_hash = packet.data[:RNS.Identity.HASHLENGTH/8]
					else:
						proof_hash = None

					# Check if this proof neds to be transported
					if packet.destination_hash in Transport.reverse_table:
						reverse_entry = Transport.reverse_table.pop(packet.destination_hash)
						if packet.receiving_interface == reverse_entry[1]:
							RNS.log("Proof received on correct interface, transporting it via "+str(reverse_entry[0]), RNS.LOG_DEBUG)
							new_raw = packet.raw[0:1]
							new_raw += struct.pack("!B", packet.hops)
							new_raw += packet.raw[2:]
							reverse_entry[0].processOutgoing(new_raw)
						else:
							RNS.log("Proof received on wrong interface, not transporting it.", RNS.LOG_DEBUG)

					for receipt in Transport.receipts:
						receipt_validated = False
						if proof_hash != None:
							# Only test validation if hash matches
							if receipt.hash == proof_hash:
								receipt_validated = receipt.validateProofPacket(packet)
						else:
							# In case of an implicit proof, we have
							# to check every single outstanding receipt
							receipt_validated = receipt.validateProofPacket(packet)

						if receipt_validated:
							Transport.receipts.remove(receipt)

		Transport.jobs_locked = False

	@staticmethod
	def registerDestination(destination):
		destination.MTU = RNS.Reticulum.MTU
		if destination.direction == RNS.Destination.IN:
			Transport.destinations.append(destination)

	@staticmethod
	def registerLink(link):
		RNS.log("Registering link "+str(link), RNS.LOG_DEBUG)
		if link.initiator:
			Transport.pending_links.append(link)
		else:
			Transport.active_links.append(link)

	@staticmethod
	def activateLink(link):
		RNS.log("Activating link "+str(link), RNS.LOG_DEBUG)
		if link in Transport.pending_links:
			Transport.pending_links.remove(link)
			Transport.active_links.append(link)
			link.status = RNS.Link.ACTIVE
		else:
			RNS.log("Attempted to activate a link that was not in the pending table", RNS.LOG_ERROR)


	@staticmethod
	def shouldCache(packet):
		# TODO: Implement sensible rules for which
		# packets to cache
		#if packet.context == RNS.Packet.RESOURCE_PRF:
		#	return True

		return False

	@staticmethod
	def cache(packet):
		if RNS.Transport.shouldCache(packet):
			try:
				packet_hash = RNS.hexrep(packet.getHash(), delimit=False)
				file = open(RNS.Reticulum.cachepath+"/"+packet_hash, "w")
				file.write(packet.raw)
				file.close()
				RNS.log("Wrote packet "+packet_hash+" to cache", RNS.LOG_EXTREME)
			except Exception as e:
				RNS.log("Error writing packet to cache", RNS.LOG_ERROR)
				RNS.log("The contained exception was: "+str(e))

	# TODO: Implement cache requests. Needs methodology
	# rethinking. This is skeleton code.
	@staticmethod
	def cache_request_packet(packet):
		if len(packet.data) == RNS.Identity.HASHLENGTH/8:
			packet_hash = RNS.hexrep(packet.data, delimit=False)
			path = RNS.Reticulum.cachepath+"/"+packet_hash
			if os.path.isfile(path):
				file = open(path, "r")
				raw = file.read()
				file.close()
				packet = RNS.Packet(None, raw)
				# TODO: Implement outbound for this


	# TODO: Implement cache requests. Needs methodology
	# rethinking. This is skeleton code.
	@staticmethod
	def cache_request(packet_hash):
		RNS.log("Cache request for "+RNS.prettyhexrep(packet_hash), RNS.LOG_EXTREME)
		path = RNS.Reticulum.cachepath+"/"+RNS.hexrep(packet_hash, delimit=False)
		if os.path.isfile(path):
			file = open(path, "r")
			raw = file.read()
			Transport.inbound(raw)
			file.close()
		else:
			cache_request_packet = RNS.Packet(Transport.transport_destination(), packet_hash, context = RNS.Packet.CACHE_REQUEST)

	@staticmethod
	def hasPath(destination_hash):
		if destination_hash in Transport.destination_table:
			return True
		else:
			return False

	@staticmethod
	def requestPath(destination_hash):
		path_request_data = destination_hash + RNS.Identity.getRandomHash()
		path_request_dst = RNS.Destination(None, RNS.Destination.OUT, RNS.Destination.PLAIN, Transport.APP_NAME, "path", "request")
		packet = RNS.Packet(path_request_dst, path_request_data, packet_type = RNS.Packet.DATA, transport_type = RNS.Transport.BROADCAST, header_type = RNS.Packet.HEADER_1)
		packet.send()

	@staticmethod
	def pathRequestHandler(data, packet):
		if len(data) >= RNS.Identity.TRUNCATED_HASHLENGTH/8:
			Transport.pathRequest(data[:RNS.Identity.TRUNCATED_HASHLENGTH/8])

	@staticmethod
	def pathRequest(destination_hash):
		RNS.log("Path request for "+RNS.prettyhexrep(destination_hash), RNS.LOG_DEBUG)
		
		local_destination = next((d for d in Transport.destinations if d.hash == destination_hash), None)
		if local_destination != None:
			RNS.log("Destination is local to this system, announcing", RNS.LOG_DEBUG)
			local_destination.announce(path_response=True)

		elif destination_hash in Transport.destination_table:
			RNS.log("Path found, inserting announce for transmission", RNS.LOG_DEBUG)
			packet = Transport.destination_table[destination_hash][6]
			received_from = Transport.destination_table[destination_hash][5]

			now = time.time()
			retries = Transport.PATHFINDER_R
			local_rebroadcasts = 0
			block_rebroadcasts = True
			retransmit_timeout = now + Transport.PATH_REQUEST_GRACE # + (RNS.rand() * Transport.PATHFINDER_RW)

			Transport.announce_table[packet.destination_hash] = [now, retransmit_timeout, retries, received_from, packet.hops, packet, local_rebroadcasts, block_rebroadcasts]

		else:
			RNS.log("No known path to requested destination, ignoring request", RNS.LOG_DEBUG)

	# TODO: Currently only used for cache requests.
	# Needs rethink.
	@staticmethod
	def transport_destination():
		# TODO: implement this
		pass

	@staticmethod
	def exitHandler():
		try:
			packet_hashlist_path = RNS.Reticulum.configdir+"/packet_hashlist"
			file = open(packet_hashlist_path, "w")
			file.write(umsgpack.packb(Transport.packet_hashlist))
			file.close()
		except Exception as e:
			RNS.log("Could not save packet hashlist to disk, the contained exception was: "+str(e), RNS.LOG_ERROR)
