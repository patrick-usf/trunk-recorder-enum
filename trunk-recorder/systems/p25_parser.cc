#include "p25_parser.h"
#include "../formatter.h"

using namespace csv;

P25Parser::P25Parser() {}


void P25Parser::load_freq_table(std::string custom_freq_table_file, int sys_num) {

  if (custom_freq_table_file == "") {
    return;
  } else {
    BOOST_LOG_TRIVIAL(info) << "Loading Custom Frequency Table File: " << custom_freq_table_file;
  }

  CSVFormat format;
  format.trim({' ', '\t'});
  CSVReader reader(custom_freq_table_file, format);
  std::vector<std::string> headers = reader.get_col_names();

  if (headers[0] != "TABLEID" || headers[1] != "TYPE" || headers[2] != "BASE" || headers[3] != "SPACING" || headers[4] != "OFFSET" ) {
    BOOST_LOG_TRIVIAL(error) << "Cuustom Frequency Table File Invalid Headers.";
  }

  for (CSVRow &row : reader) { // Input iterator
    unsigned long id = row["TABLEID"].get<unsigned long>() - 1; //CSV Format is One-Based, but the Standard is Zero-Based.
    std::string type = row["TYPE"].get<std::string>();
    unsigned long frequency = row["BASE"].get<double>() * 1000000;
    unsigned long step = row["SPACING"].get<double>() * 1000;

    // Need to remove the "+" sign from positive numbers if it exists.
    std::string offset_string = row["OFFSET"].get<std::string>();
    if(offset_string[0] == '+'){
      offset_string = offset_string.substr(1);
    }

    long offset = std::stol(offset_string) * 1000000;

    Freq_Table temp_table;
    temp_table.id = id;
    temp_table.frequency = frequency;
    temp_table.step = step;
    temp_table.offset = offset;

    if(type == "FDMA"){
      temp_table.phase2_tdma = false;
      temp_table.slots_per_carrier = 1;
      temp_table.bandwidth = 12.5;
    }
    else
    {
      temp_table.phase2_tdma = true;
      temp_table.slots_per_carrier = 2;
      temp_table.bandwidth = 6.25;
    }

    BOOST_LOG_TRIVIAL(info) << "Adding Frequency Table:\t" << id << "\t" << type << "\t" << frequency << "\t" << step << "\t" << offset;
  
    add_freq_table(id, temp_table, sys_num);
  }

  custom_freq_table_loaded = true;

}

void P25Parser::add_freq_table(int freq_table_id, Freq_Table temp_table, int sys_num) {
  /*std::cout << "Add  - Channel id " << std::dec << chan_id << " freq " <<
    temp_table.frequency << " offset " << temp_table.offset << " step " <<
   temp_table.step << " slots/carrier " << temp_table.slots_per_carrier  << std::endl;
*/
  freq_tables[sys_num][freq_table_id] = temp_table;
}

long P25Parser::get_tdma_slot(int chan_id, int sys_num) {
  long channel = chan_id & 0xfff;

  it = freq_tables[sys_num].find((chan_id >> 12) & 0xf);

  if (it != freq_tables[sys_num].end()) {
    Freq_Table temp_table = it->second;

    if (temp_table.phase2_tdma) {
      return channel & 1;
    }
  }

  return -1;
}

double P25Parser::get_bandwidth(int chan_id, int sys_num) {
  it = freq_tables[sys_num].find((chan_id >> 12) & 0xf);

  if (it != freq_tables[sys_num].end()) {
    Freq_Table temp_table = it->second;
    return temp_table.bandwidth;
  }

  return 0;
}

double P25Parser::channel_id_to_frequency(int chan_id, int sys_num) {
  // long id      = (chan_id >> 12) & 0xf;
  long channel = chan_id & 0xfff;

  it = freq_tables[sys_num].find((chan_id >> 12) & 0xf);

  if (it != freq_tables[sys_num].end()) {
    Freq_Table temp_table = it->second;

    if (temp_table.phase2_tdma) {
      return temp_table.frequency + temp_table.step * int(channel / temp_table.slots_per_carrier);
    } else {
      return temp_table.frequency + temp_table.step * channel;
    }
  }
  return 0;
}

std::string P25Parser::channel_id_to_freq_string(int chan_id, int sys_num) {
  double f = channel_id_to_frequency(chan_id, sys_num);

  if (f == 0) {
    return "ID"; // << std::hex << chan_id;
  } else {
    std::ostringstream strs;
    strs << f / 1000000.0;
    return strs.str();
  }
}

std::string P25Parser::channel_to_string(int chan, int sys_num) {

  long bandplan = (chan >> 12) & 0xf;
  long channel = chan & 0xfff;

  std::ostringstream strs;
  strs << std::setfill('0') << std::setw(2) << bandplan << "-" << std::setfill('0') << std::setw(4) << channel;
  return strs.str();
}

unsigned long P25Parser::bitset_shift_mask(boost::dynamic_bitset<> &tsbk, int shift, unsigned long long mask) {
  boost::dynamic_bitset<> bitmask(tsbk.size(), mask);
  unsigned long result = ((tsbk >> shift) & bitmask).to_ulong();

  // std::cout << "    " << std::dec<< shift << " " << tsbk.size() << " [ " <<
  // mask << " ]  = " << result << " - " << ((tsbk >> shift) & bitmask) <<
  // std::endl;
  return result;
}

unsigned long P25Parser::bitset_shift_left_mask(boost::dynamic_bitset<> &tsbk, int shift, unsigned long long mask) {
  boost::dynamic_bitset<> bitmask(tsbk.size(), mask);
  unsigned long result = ((tsbk << shift) & bitmask).to_ulong();

  // std::cout << "    " << std::dec<< shift << " " << tsbk.size() << " [ " <<
  // mask << " ]  = " << result << " - " << ((tsbk >> shift) & bitmask) <<
  // std::endl;
  return result;
}

std::vector<TrunkMessage> P25Parser::decode_mbt_data(unsigned long opcode, boost::dynamic_bitset<> &header, boost::dynamic_bitset<> &mbt_data, unsigned long sa, unsigned long nac, int sys_num) {
  std::vector<TrunkMessage> messages;
  TrunkMessage message;
  std::ostringstream os;

  message.message_type = UNKNOWN;
  message.source = -1;
  message.wacn = 0;
  message.nac = nac;
  message.sys_id = 0;
  message.sys_rfss = 0;
  message.sys_site_id = 0;
  message.sys_num = sys_num;
  message.talkgroup = 0;
  message.emergency = false;
  message.encrypted = false;
  message.duplex = false;
  message.mode = false;
  message.priority = 0;
  message.phase2_tdma = false;
  message.tdma_slot = 0;
  message.freq = 0;
  message.mfid = bitset_shift_mask(header, 72, 0xff);
  // io bit: header byte 2, bit 7 (MSB) = 1 → ISP (radio→FNE), 0 → OSP (FNE→radio).
  // In the bitset (LSB-first, <<16 shifted): byte 2 bit 7 is at position 79.
  bool mbt_isp = (bool)bitset_shift_mask(header, 79, 0x1);
  message.direction = mbt_isp ? DIR_ISP : DIR_OSP;
  // ISP MBT opcodes share numeric values with OSP opcodes; tag them with bit 6 so the
  // opcode_name() lookup in p25_frame_logger can distinguish GRP_AFF_REQ (0x68) from
  // GRP_AFF_RSP (0x28), etc.
  message.opcode = mbt_isp ? (opcode | 0x40) : opcode;
  message.patch_data.sg = 0;
  message.patch_data.ga1 = 0;
  message.patch_data.ga2 = 0;
  message.patch_data.ga3 = 0;

  BOOST_LOG_TRIVIAL(debug) << "decode_mbt_data: $" << opcode
                           << " dir=" << (message.direction == DIR_ISP ? "ISP" : "OSP");
  if (opcode == 0x0) { // grp voice channel grant
    // unsigned long mfrid = bitset_shift_mask(header, 72, 0xff);
    unsigned long ch1 = bitset_shift_mask(mbt_data, 64, 0xffff);
    unsigned long ch2 = bitset_shift_mask(mbt_data, 48, 0xffff);
    unsigned long ga = bitset_shift_mask(mbt_data, 32, 0xffff);
    unsigned long f1 = channel_id_to_frequency(ch1, sys_num);
    unsigned long f2 = channel_id_to_frequency(ch2, sys_num);
    unsigned long sa = bitset_shift_mask(header, 48, 0xffffff);
    bool emergency = (bool)bitset_shift_mask(header, 24, 0x80);
    bool encrypted = (bool)bitset_shift_mask(header, 24, 0x40);
    bool duplex = (bool)bitset_shift_mask(header, 24, 0x20);
    bool mode = (bool)bitset_shift_mask(header, 24, 0x10);
    int priority = bitset_shift_mask(header, 24, 0x07);


    message.message_type = GRANT;
    message.freq = f1;
    message.talkgroup = ga;
    message.source = sa;
    message.emergency = emergency;
    message.encrypted = encrypted;
    message.duplex = duplex;
    message.mode = mode;
    message.priority = priority;

    if (get_tdma_slot(ch1, sys_num) >= 0) {
      message.phase2_tdma = true;
      message.tdma_slot = get_tdma_slot(ch1, sys_num);
    } else {
      message.phase2_tdma = false;
      message.tdma_slot = 0;
    }

    os << "mbt00\tChan Grant\tChannel 1 ID: " << channel_to_string(ch1, sys_num) << "\tFreq: " << format_freq(f1) <<  "\tChannel 2 ID: " << channel_to_string(ch2, sys_num) << "\tFreq: " << format_freq(f2) << "\tga " << std::setw(7) << ga << "\tTDMA " << get_tdma_slot(ch1, sys_num) << "\tsa " << sa << "\tEncrypt " << encrypted << "\tBandwidth: " << get_bandwidth(ch1, sys_num);
    message.meta = os.str();
    BOOST_LOG_TRIVIAL(debug) << os.str();
  } else if (opcode == 0x02) { // grp regroup voice channel grant
    unsigned long mfrid = bitset_shift_mask(mbt_data, 168, 0xff);
    if (mfrid == 0x90) {  // MOT_GRG_CN_GRANT_EXP
      unsigned long ch1 = bitset_shift_mask(mbt_data, 80, 0xffff);
      unsigned long ch2 = bitset_shift_mask(mbt_data, 64, 0xffff);
      unsigned long sg = bitset_shift_mask(mbt_data, 48, 0xffff);
      unsigned long f1 = channel_id_to_frequency(ch1, sys_num);
      unsigned long f2 = channel_id_to_frequency(ch2, sys_num);
      message.message_type = GRANT;
      message.freq = f1;
      message.talkgroup = sg;

      if (get_tdma_slot(ch1, sys_num) >= 0) {
        message.phase2_tdma = true;
        message.tdma_slot = get_tdma_slot(ch1, sys_num);
      } else {
        message.phase2_tdma = false;
        message.tdma_slot = 0;
      }

      os << "mbt02\tmfid90_grg_cn_grant_exp\tChannel 1 ID: " << channel_to_string(ch1, sys_num) << "\tFreq: " << format_freq(f1) <<  "\tChannel 2 ID: " << channel_to_string(ch2, sys_num) << "\tFreq: " << format_freq(f2) << "\tsg " << std::setw(7) << sg << "\tTDMA " << get_tdma_slot(ch1, sys_num) << "\tBandwidth: " << get_bandwidth(ch1, sys_num);
      message.meta = os.str();
      BOOST_LOG_TRIVIAL(debug) << os.str();
    }
  } else if (opcode == 0x028) {
    if (message.direction == DIR_ISP) {
      // ISP: GRP_AFF_REQ — group affiliation request from MDT.
      // Data block fields (wacn, syid, ga, src_id) require data blocks that are often
      // absent in uplink captures. Log what is available from the header.
      unsigned long src = bitset_shift_mask(header, 48, 0xffffff);
      os << "mbt28_isp\tGRP_AFF_REQ:\tsrc=" << src;
      if (mbt_data.size() > 64) {
        unsigned long wacn = bitset_shift_mask(mbt_data, 188, 0xfffff);
        unsigned long syid = bitset_shift_mask(mbt_data, 176, 0xfff);
        unsigned long ga   = bitset_shift_mask(mbt_data, 128, 0xffff);
        os << "\twacn=0x" << std::hex << wacn << "\tsyid=0x" << syid << "\tga=" << std::dec << ga;
        message.talkgroup = ga;
        message.source = src;
      }
      message.meta = os.str();
      BOOST_LOG_TRIVIAL(debug) << os.str();
    } else {
      // OSP: GRP_AFF_RSP — group affiliation response from FNE.
      unsigned long mfrid = bitset_shift_mask(mbt_data, 56, 0xff);
      unsigned long wacn = (bitset_shift_left_mask(header, 4, 0xffff0) + bitset_shift_mask(mbt_data, 188, 0xf));
      unsigned long syid = bitset_shift_mask(mbt_data, 176, 0xfff);
      unsigned long gid = bitset_shift_mask(mbt_data, 160, 0xffff);
      unsigned long ada = bitset_shift_mask(mbt_data, 144, 0xffff);
      unsigned long ga = bitset_shift_mask(mbt_data, 128, 0xffff);
      unsigned long lg = bitset_shift_mask(mbt_data, 127, 0x1);
      unsigned long gav = bitset_shift_mask(mbt_data, 120, 0x3);
      os << "mbt28\tmbt(0x28) grp_aff_rsp:\tMFRID: " << mfrid <<  "\tWACN: " <<  wacn << "\tSYID: " << syid << "\tLG: " << lg << "\tGAV: " << gav << "\tADA: " << ada << "\tGA: " << ga << "\tLG: " << lg << "\tGID: " << gid;
      message.meta = os.str();
      BOOST_LOG_TRIVIAL(debug) << os.str();
    }
  } else if (opcode == 0x3a) { // rfss status
    unsigned long syid = bitset_shift_mask(header, 48, 0xfff);
    unsigned long rfid = bitset_shift_mask(mbt_data, 88, 0xff);
    unsigned long stid = bitset_shift_mask(mbt_data, 80, 0xff);
    unsigned long ch1 = bitset_shift_mask(mbt_data, 64, 0xffff);
    // unsigned long ch2 = bitset_shift_mask(mbt_data, 48, 0xffff);
    // unsigned long f1   = channel_id_to_frequency(ch1, sys_num);
    // unsigned long f2   = channel_id_to_frequency(ch2, sys_num);
    message.message_type = SYSID;
    message.sys_id = syid;
    message.sys_rfss = rfid;
    message.sys_site_id = stid;
    os << "mbt3a rfss status: syid: " << syid << " rfid " << rfid << " stid " << stid << " ch1 " << channel_to_string(ch1, sys_num) << "(" << channel_id_to_freq_string(ch1, sys_num) << ")";
    message.meta = os.str();
    BOOST_LOG_TRIVIAL(debug) << os.str();
  } else if (opcode == 0x3b) { // network status
    unsigned long wacn = bitset_shift_mask(mbt_data, 76, 0xfffff);
    unsigned long syid = bitset_shift_mask(header, 48, 0xfff);
    unsigned long ch1 = bitset_shift_mask(mbt_data, 56, 0xffff);
    unsigned long ch2 = bitset_shift_mask(mbt_data, 40, 0xffff);
    unsigned long f1 = channel_id_to_frequency(ch1, sys_num);
    unsigned long f2 = channel_id_to_frequency(ch2, sys_num);

    if (f1 && f2) {
      message.message_type = STATUS;
      message.wacn = wacn;
      message.sys_id = syid;
      message.freq = f1;
    }
    os << "net_sts wacn=0x" << std::hex << std::setfill('0') << std::setw(5) << wacn
       << " syid=0x" << std::setw(3) << syid
       << " ch1=" << channel_to_string(ch1, sys_num) << "(" << channel_id_to_freq_string(ch1, sys_num) << ")"
       << " ch2=" << channel_to_string(ch2, sys_num) << "(" << channel_id_to_freq_string(ch2, sys_num) << ")";
    message.meta = os.str();
    BOOST_LOG_TRIVIAL(debug) << "mbt3b " << os.str();
  } else if (opcode == 0x3c) { // adjacent status
    unsigned long syid = bitset_shift_mask(header, 48, 0xfff);
    unsigned long rfid = bitset_shift_mask(header, 24, 0xff);
    unsigned long stid = bitset_shift_mask(header, 16, 0xff);
    unsigned long ch1 = bitset_shift_mask(mbt_data, 80, 0xffff);
    unsigned long ch2 = bitset_shift_mask(mbt_data, 64, 0xffff);
    unsigned long f1  = channel_id_to_frequency(ch1, sys_num);
    unsigned long f2  = channel_id_to_frequency(ch2, sys_num);
    os << "adj_sts syid=0x" << std::hex << std::setfill('0') << std::setw(3) << syid
       << " rfid=" << std::dec << rfid << " stid=" << stid
       << " ch1=" << channel_to_string(ch1, sys_num) << "(" << channel_id_to_freq_string(ch1, sys_num) << ")"
       << " ch2=" << channel_to_string(ch2, sys_num) << "(" << channel_id_to_freq_string(ch2, sys_num) << ")";
    message.meta = os.str();
    BOOST_LOG_TRIVIAL(debug) << "mbt3c " << os.str();
  } else if (opcode == 0x04) { //  Unit to Unit Voice Service Channel Grant -Extended (UU_V_CH_GRANT)
    // unsigned long mfrid = bitset_shift_mask(header, 80, 0xff);
    bool emergency = (bool)bitset_shift_mask(header, 24, 0x80);
    bool encrypted = (bool)bitset_shift_mask(header, 24, 0x40);
    bool dup = (bool)bitset_shift_mask(header, 24, 0x20);
    bool mod = (bool)bitset_shift_mask(header, 24, 0x10);
    int pri = bitset_shift_mask(header, 24, 0x07);
    unsigned long ch = bitset_shift_mask(header, 16, 0xffff); /// ????
    unsigned long f = channel_id_to_frequency(ch, sys_num);
    unsigned long sa = bitset_shift_mask(header, 48, 0xffffff);
    unsigned long ta = bitset_shift_mask(mbt_data, 24, 0xffffff);

    message.message_type = UU_V_GRANT;
    message.freq = f;
    message.talkgroup = ta;
    message.source = sa;
    message.emergency = emergency;
    message.encrypted = encrypted;
    message.duplex = dup;
    message.mode = mod;
    message.priority = pri;
    if (get_tdma_slot(ch, sys_num) >= 0) {
      message.phase2_tdma = true;
      message.tdma_slot = get_tdma_slot(ch, sys_num);
    } else {
      message.phase2_tdma = false;
      message.tdma_slot = 0;
    }

    BOOST_LOG_TRIVIAL(debug) << "mbt04\tUnit to Unit Chan Grant\tChannel ID: " << channel_to_string(ch, sys_num) << "\tFreq: " << format_freq(f) << "\tTarget ID: " << std::setw(7) << ta << "\tTDMA " << get_tdma_slot(ch, sys_num) << "\tSource ID: " << sa;
  } else {
    BOOST_LOG_TRIVIAL(debug) << "mbt_unknown: op=0x" << std::hex << opcode
                             << " mfid=0x" << message.mfid;
    std::ostringstream raw;
    raw << "op=0x" << std::hex << std::setfill('0') << std::setw(2) << opcode
        << " mfid=0x" << std::setw(2) << message.mfid;
    message.raw_frame = raw.str();
    message.meta = "unknown_mbt op=0x" + [&]{ std::ostringstream s; s << std::hex << opcode; return s.str(); }();
    messages.push_back(message);
    return messages;
  }
  // Populate raw_frame for every MBT frame that didn't already set it — same
  // pattern as decode_tsbk: covers both stub branches and fully-parsed frames.
  if (message.raw_frame.empty()) {
    std::ostringstream raw;
    raw << "op=0x" << std::hex << std::setfill('0') << std::setw(2) << opcode
        << " mfid=0x" << std::setw(2) << message.mfid << " hdr=";
    boost::dynamic_bitset<> tmp = header >> 16;
    int hdr_bytes = (int)(header.size() - 16) / 8;
    for (int i = hdr_bytes - 1; i >= 0; i--) {
      uint8_t b = 0;
      for (int j = 7; j >= 0; j--)
        b = (b << 1) | (unsigned int)tmp[i * 8 + j];
      raw << std::setw(2) << (unsigned int)b;
    }
    message.raw_frame = raw.str();
    if (message.meta.empty()) {
      std::ostringstream ms;
      ms << "stub_mbt op=0x" << std::hex << opcode;
      message.meta = ms.str();
    }
  }
  messages.push_back(message);
  return messages;
}

std::vector<TrunkMessage> P25Parser::decode_tsbk(boost::dynamic_bitset<> &tsbk, unsigned long nac, int sys_num) {
  // self.stats['tsbks'] += 1
  std::vector<TrunkMessage> messages;
  TrunkMessage message;
  std::ostringstream os;

  // TSBK is shifted 16 prior for the missing CRC prior to this function
  unsigned long opcode = bitset_shift_mask(tsbk, 88, 0x3f); // x3f

  message.message_type = UNKNOWN;
  message.source = -1;
  message.wacn = 0;
  message.nac = nac;
  message.sys_id = 0;
  message.sys_rfss = 0;
  message.sys_site_id = 0;
  message.sys_num = sys_num;
  message.talkgroup = 0;
  message.emergency = false;
  message.duplex = false;
  message.mode = false;
  message.priority = 0;
  message.encrypted = false;
  message.phase2_tdma = false;
  message.tdma_slot = 0;
  message.freq = 0;
  message.opcode = opcode;
  message.mfid = bitset_shift_mask(tsbk, 80, 0xff);
  message.direction = DIR_OSP;
  message.patch_data.sg = 0;
  message.patch_data.ga1 = 0;
  message.patch_data.ga2 = 0;
  message.patch_data.ga3 = 0;

  BOOST_LOG_TRIVIAL(trace) << "TSBK: opcode: $" << std::hex << opcode;

  // ISP (Radio→FNE): PI bit (bit 6 of byte 0) set — route before OSP chain to
  // avoid collisions where ISP and OSP share the same 6-bit opcode value.
  unsigned long lb = bitset_shift_mask(tsbk, 95, 0x01);
  unsigned long pi = bitset_shift_mask(tsbk, 94, 0x01);

  // Apply structured {OSP/ISP:[FS][NAC][DUID]}{TSBK:[LB][PF][Opcode][MFID] inner} wrapper
  // Called just before every return in this function.
  auto apply_tsbk_wrapper = [&]() {
    for (auto &m : messages) {
      std::ostringstream hdr;
      hdr << std::hex << std::setfill('0')
          << (pi ? "{ISP:" : "{OSP:")
          << "[FS=0x5575F5FF77FF][NAC=0x" << std::setw(3) << nac
          << "][DUID=0x07]}{TSBK:[LB=" << std::dec << lb
          << "][PF=" << pi
          << "][Opcode=0x" << std::hex << std::setw(2) << m.opcode
          << "][MFID=0x" << std::setw(2) << m.mfid
          << "]" << m.meta << "}";
      m.meta = hdr.str();
    }
  };

  if (pi) {
    message.direction = DIR_ISP;
    message.opcode    = 0x40 | opcode; // PI-extended for TSV logging (matches op25 convention)

    if (opcode == 0x00) { // 0x40 GRP_V_CH_REQ
      unsigned long sa = bitset_shift_mask(tsbk, 48, 0xffffff);
      unsigned long ga = bitset_shift_mask(tsbk, 32, 0xffff);
      message.source    = sa;
      message.talkgroup = ga;
      os << "grp_v_ch_req wuid=" << std::dec << sa << " tg=" << ga;
      message.meta = os.str();
      BOOST_LOG_TRIVIAL(debug) << "tsbk40 " << os.str();
    } else if (opcode == 0x04) { // 0x44 UU_V_CH_REQ
      unsigned long sa   = bitset_shift_mask(tsbk, 48, 0xffffff);
      unsigned long dest = bitset_shift_mask(tsbk, 24, 0xffffff);
      message.source    = sa;
      message.talkgroup = dest;
      os << "uu_v_ch_req wuid=" << std::dec << sa << " dest=" << dest;
      message.meta = os.str();
      BOOST_LOG_TRIVIAL(debug) << "tsbk44 " << os.str();
    } else if (opcode == 0x05) { // 0x45 UU_ANS_RSP — unit-to-unit answer response
      unsigned long opts = bitset_shift_mask(tsbk, 72, 0xff);
      unsigned long sa   = bitset_shift_mask(tsbk, 48, 0xffffff);
      unsigned long da   = bitset_shift_mask(tsbk, 24, 0xffffff);
      message.source    = sa;
      message.talkgroup = da;
      os << "uu_ans_rsp wuid=" << std::dec << sa << " dest=" << da
         << " opts=0x" << std::hex << std::setfill('0') << std::setw(2) << opts;
      message.meta = os.str();
      BOOST_LOG_TRIVIAL(debug) << "tsbk45 " << os.str();
    } else if (opcode == 0x14) { // 0x54 — ISP opcode 0x14 (identity uncertain; no captures)
      unsigned long sa = bitset_shift_mask(tsbk, 48, 0xffffff);
      message.source = sa;
      os << "isp_0x14 wuid=" << std::dec << sa;
      message.meta = os.str();
      BOOST_LOG_TRIVIAL(debug) << "tsbk54 " << os.str();
    } else if (opcode == 0x15) { // 0x55 SNDCP_CH_REQ (true uplink; PI=0 echo handled below)
      unsigned long svcopt = bitset_shift_mask(tsbk, 72, 0xff);
      unsigned long sa     = bitset_shift_mask(tsbk, 16, 0xffffff);
      message.source = sa;
      os << "sndcp_req wuid=" << std::dec << sa
         << " svcopt=0x" << std::hex << std::setfill('0') << std::setw(2) << svcopt;
      message.meta = os.str();
      BOOST_LOG_TRIVIAL(debug) << "tsbk55 " << os.str();
    } else if (opcode == 0x16) { // 0x56 GRP_AFF_REQ
      unsigned long ga = bitset_shift_mask(tsbk, 48, 0xffff);
      unsigned long sa = bitset_shift_mask(tsbk, 24, 0xffffff);
      message.source    = sa;
      message.talkgroup = ga;
      os << "grp_aff_req wuid=" << std::dec << sa << " tg=" << ga;
      message.meta = os.str();
      BOOST_LOG_TRIVIAL(debug) << "tsbk56 " << os.str();
    } else if (opcode == 0x17) { // 0x57 U_DEREG_REQ
      unsigned long sa = bitset_shift_mask(tsbk, 32, 0xffffff);
      message.source = sa;
      os << "u_dereg_req wuid=" << std::dec << sa;
      message.meta = os.str();
      BOOST_LOG_TRIVIAL(debug) << "tsbk57 " << os.str();
    } else if (opcode == 0x18) { // 0x58 LOC_REG_REQ
      unsigned long sa = bitset_shift_mask(tsbk, 32, 0xffffff);
      message.source = sa;
      os << "loc_reg_req wuid=" << std::dec << sa;
      message.meta = os.str();
      BOOST_LOG_TRIVIAL(debug) << "tsbk58 " << os.str();
    } else if (opcode == 0x1a) { // 0x5A U_REG_REQ
      unsigned long sa = bitset_shift_mask(tsbk, 32, 0xffffff);
      message.source = sa;
      os << "u_reg_req wuid=" << std::dec << sa;
      message.meta = os.str();
      BOOST_LOG_TRIVIAL(debug) << "tsbk5a " << os.str();
    } else if (opcode == 0x1b) { // 0x5B AUTH_RESP — authentication response from radio (TIA-102.AACA)
      unsigned long sa       = bitset_shift_mask(tsbk, 48, 0xffffff);
      unsigned long auth_res = bitset_shift_mask(tsbk, 16, 0xffffffff);
      message.source = sa;
      os << "auth_resp wuid=" << std::dec << sa
         << " res=0x" << std::hex << std::setfill('0') << std::setw(8) << auth_res;
      message.meta = os.str();
      BOOST_LOG_TRIVIAL(debug) << "tsbk5b " << os.str();
    } else if (opcode == 0x1c) { // 0x5C AUTH_FNE_RSP — radio auth response to FNE challenge (TIA-102.AACA)
      unsigned long sa       = bitset_shift_mask(tsbk, 48, 0xffffff);
      unsigned long auth_res = bitset_shift_mask(tsbk, 16, 0xffffffff);
      message.source = sa;
      os << "auth_fne_rsp wuid=" << std::dec << sa
         << " res=0x" << std::hex << std::setfill('0') << std::setw(8) << auth_res;
      message.meta = os.str();
      BOOST_LOG_TRIVIAL(debug) << "tsbk5c " << os.str();
    } else {
      BOOST_LOG_TRIVIAL(debug) << "tsbk_isp_unknown: op=0x" << std::hex << message.opcode
                               << " mfid=0x" << message.mfid;
    }

    // Capture raw bytes for all ISP frames
    {
      std::ostringstream raw;
      raw << "op=0x" << std::hex << std::setfill('0') << std::setw(2) << (unsigned int)message.opcode
          << " mfid=0x" << std::setw(2) << message.mfid << " bytes=";
      boost::dynamic_bitset<> tmp = tsbk >> 16;
      for (int _i = 11; _i >= 0; _i--) {
        uint8_t _b = 0;
        for (int _j = 7; _j >= 0; _j--)
          _b = (_b << 1) | (unsigned int)tmp[_i * 8 + _j];
        raw << std::hex << std::setfill('0') << std::setw(2) << (unsigned int)_b;
      }
      message.raw_frame = raw.str();
    }
    if (message.meta.empty()) {
      std::ostringstream ms;
      ms << "isp_tsbk op=0x" << std::hex << (unsigned int)message.opcode;
      message.meta = ms.str();
    }
    messages.push_back(message);
    apply_tsbk_wrapper();
    return messages;
  }

  if (opcode == 0x00) { // group voice chan grant
    // Group Voice Channel Grant (GRP_V_CH_GRANT)

    unsigned long mfrid = bitset_shift_mask(tsbk, 80, 0xff);

    if (mfrid == 0x90) { // MOT_GRG_ADD_CMD
      unsigned long sg = bitset_shift_mask(tsbk, 64, 0xffff);
      unsigned long ga1 = bitset_shift_mask(tsbk, 48, 0xffff);
      unsigned long ga2 = bitset_shift_mask(tsbk, 32, 0xffff);
      unsigned long ga3 = bitset_shift_mask(tsbk, 16, 0xffff);
      BOOST_LOG_TRIVIAL(debug) << "tsbk00\tMoto Patch Add \tsg: " << sg << "\tga1: " << ga1 << "\tga2: " << ga2 << "\tga3: " << ga3;
      message.message_type = PATCH_ADD;
      PatchData moto_patch_data;
      moto_patch_data.sg = sg;
      moto_patch_data.ga1 = ga1;
      moto_patch_data.ga2 = ga2;
      moto_patch_data.ga3 = ga3;
      message.patch_data = moto_patch_data;
    } else {
      // unsigned long opts  = bitset_shift_mask(tsbk, 72, 0xff); // not required for anything 
      bool emergency = (bool)bitset_shift_mask(tsbk, 72, 0x80);
      bool encrypted = (bool)bitset_shift_mask(tsbk, 72, 0x40);
      bool duplex = (bool)bitset_shift_mask(tsbk, 72, 0x20);
      bool mode = (bool)bitset_shift_mask(tsbk, 72, 0x10);
      int priority = bitset_shift_mask(tsbk, 72, 0x07);
      unsigned long ch = bitset_shift_mask(tsbk, 56, 0xffff);
      unsigned long ga = bitset_shift_mask(tsbk, 40, 0xffff);
      unsigned long sa = bitset_shift_mask(tsbk, 16, 0xffffff);
      unsigned long f1 = channel_id_to_frequency(ch, sys_num);
      message.message_type = GRANT;
      message.freq = f1;
      message.talkgroup = ga;
      message.source = sa;
      message.emergency = emergency;
      message.encrypted = encrypted;
      message.duplex = duplex;
      message.mode = mode;
      message.priority = priority;

      if (get_tdma_slot(ch, sys_num) >= 0) {
        message.phase2_tdma = true;
        message.tdma_slot = get_tdma_slot(ch, sys_num);
      } else {
        message.phase2_tdma = false;
        message.tdma_slot = 0;
      }
      os << "tsbk00\tChan Grant\tChannel ID: " << channel_to_string(ch, sys_num) << "\tFreq: " << format_freq(f1) << "\tga " << std::setw(7) << ga << "\tTDMA " << get_tdma_slot(ch, sys_num) << "\tsa " << sa << "\tEncrypt " << encrypted << "\tBandwidth: " << get_bandwidth(ch, sys_num);
      message.meta = os.str();
      BOOST_LOG_TRIVIAL(debug) << os.str();
    }
  } else if (opcode == 0x02) { // group voice chan grant update
    unsigned long mfrid = bitset_shift_mask(tsbk, 80, 0xff);
    // Group Voice Channel Grant Update (GRP_V_CH_GRANT_UPDT) : TIA.102-AABC-B-2005 page 34
    // Options are not present in an UPDATE

    if (mfrid == 0x90) {
        // unsigned long opts = bitset_shift_mask(tsbk, 72, 0xff);  // not required for anything
        bool emergency = (bool)bitset_shift_mask(tsbk, 72, 0x80);
        bool encrypted = (bool)bitset_shift_mask(tsbk, 72, 0x40);
        bool duplex = (bool)bitset_shift_mask(tsbk, 72, 0x20);
        bool mode = (bool)bitset_shift_mask(tsbk, 72, 0x10);
        int priority = bitset_shift_mask(tsbk, 72, 0x07);
        
        unsigned long ch = bitset_shift_mask(tsbk, 56, 0xffff);
        unsigned long sg = bitset_shift_mask(tsbk, 40, 0xffff);
        unsigned long sa = bitset_shift_mask(tsbk, 16, 0xffffff);
        unsigned long f = channel_id_to_frequency(ch, sys_num);

        message.message_type = GRANT;
        message.freq = f;
        message.talkgroup = sg;
        message.source = sa;
        
        message.encrypted = encrypted;
        message.emergency = emergency;
        message.duplex = duplex;
        message.mode = mode;
        message.priority = priority;

      if (get_tdma_slot(ch, sys_num) >= 0) {
        message.phase2_tdma = true;
        message.tdma_slot = get_tdma_slot(ch, sys_num);
      } else {
        message.phase2_tdma = false;
        message.tdma_slot = 0;
      }

      os << "tsbk02\tMOTOROLA_OSP_PATCH_GROUP_CHANNEL_GRANT\tChannel ID: " << channel_to_string(ch, sys_num) << "\tFreq: " << format_freq(f) << "\tsg " << std::setw(7) << sg << "\tTDMA " << get_tdma_slot(ch, sys_num) << "\tsa " << sa;
      message.meta = os.str();
      BOOST_LOG_TRIVIAL(debug) << os.str();
    } else {
      unsigned long ch1 = bitset_shift_mask(tsbk, 64, 0xffff);
      unsigned long ga1 = bitset_shift_mask(tsbk, 48, 0xffff);
      unsigned long ch2 = bitset_shift_mask(tsbk, 32, 0xffff);
      unsigned long ga2 = bitset_shift_mask(tsbk, 16, 0xffff);
      unsigned long f1 = channel_id_to_frequency(ch1, sys_num);
      unsigned long f2 = channel_id_to_frequency(ch2, sys_num);

      message.message_type = UPDATE;
      message.freq = f1;
      message.talkgroup = ga1;

      if (get_tdma_slot(ch1, sys_num) >= 0) {
        message.phase2_tdma = true;
        message.tdma_slot = get_tdma_slot(ch1, sys_num);
      } else {
        message.phase2_tdma = false;
        message.tdma_slot = 0;
      }

      if ((f1 != f2) && (ch2 != 65535)) {
        messages.push_back(message);
        message.freq = f2;
        message.talkgroup = ga2;

        if (get_tdma_slot(ch2, sys_num) >= 0) {
          message.phase2_tdma = true;
          message.tdma_slot = get_tdma_slot(ch2, sys_num);
        } else {
          message.phase2_tdma = false;
          message.tdma_slot = 0;
        }

        os << "tsbk02\tGrant Update 2nd\tChannel ID: " << channel_to_string(ch2, sys_num) << "\tFreq: " << format_freq(f2) << "\tga " << std::setw(7) << ga2 << "\tTDMA " << get_tdma_slot(ch2, sys_num) << " | ";

        message.meta = os.str();
        
      }
      os << "tsbk02\tGrant Update\tChannel ID: " << channel_to_string(ch1, sys_num) << "\tFreq: " << format_freq(f1) << "\tga " << std::setw(7) << ga1 << "\tTDMA " << get_tdma_slot(ch1, sys_num);
      message.meta = os.str();
      BOOST_LOG_TRIVIAL(debug) << os.str();
    }
  } else if (opcode == 0x03) { //  Group Voice Channel Update-Explicit (GRP_V_CH_GRANT_UPDT_EXP)
    // group voice chan grant update exp : TIA.102-AABC-B-2005 page 56
    unsigned long mfrid = bitset_shift_mask(tsbk, 80, 0xff);

    if (mfrid == 0x90) { // MOT_GRG_CN_GRANT_UPDT  // MOTOROLA_OSP_PATCH_GROUP_CHANNEL_GRANT_UPDATE // Service Options are not in the Moto version of the message

      unsigned long ch1 = bitset_shift_mask(tsbk, 64, 0xffff);
      unsigned long sg1 = bitset_shift_mask(tsbk, 48, 0xffff);
      unsigned long ch2 = bitset_shift_mask(tsbk, 32, 0xffff);
      unsigned long sg2 = bitset_shift_mask(tsbk, 16, 0xffff);

      unsigned long f1 = channel_id_to_frequency(ch1, sys_num);
      unsigned long f2 = channel_id_to_frequency(ch2, sys_num);

      message.message_type = UPDATE;
      message.freq = f1;
      message.talkgroup = sg1;

      if (get_tdma_slot(ch1, sys_num) >= 0) {
        message.phase2_tdma = true;
        message.tdma_slot = get_tdma_slot(ch1, sys_num);
      } else {
        message.phase2_tdma = false;
        message.tdma_slot = 0;
      }

      if (f1 != f2) {
        messages.push_back(message);
        message.freq = f2;
        message.talkgroup = sg2;
        if (get_tdma_slot(ch2, sys_num) >= 0) {
          message.phase2_tdma = true;
          message.tdma_slot = get_tdma_slot(ch2, sys_num);
        } else {
          message.phase2_tdma = false;
          message.tdma_slot = 0;
        }
        os << "MOTOROLA_OSP_PATCH_GROUP_CHANNEL_GRANT_UPDATE(0x03): \tChannel ID: " << channel_to_string(ch2, sys_num) << "\tFreq: " << format_freq(f2) << "\tsg " << std::setw(7) << sg2 << "\tTDMA " << get_tdma_slot(ch2, sys_num);
        message.meta = os.str();
        BOOST_LOG_TRIVIAL(debug) << os.str();
      }
      os << "MOTOROLA_OSP_PATCH_GROUP_CHANNEL_GRANT_UPDATE(0x03): \tChannel ID: " << channel_to_string(ch1, sys_num) << "\tFreq: " << format_freq(f1) << "\tsg " << std::setw(7) << sg1 << "\tTDMA " << get_tdma_slot(ch1, sys_num);
      message.meta = os.str();
      BOOST_LOG_TRIVIAL(debug) << os.str();
    } else {
      bool emergency = (bool)bitset_shift_mask(tsbk, 72, 0x80);
      bool encrypted = (bool)bitset_shift_mask(tsbk, 72, 0x40);
      // bool duplex = (bool)bitset_shift_mask(tsbk, 72, 0x20);
      // bool mode = (bool)bitset_shift_mask(tsbk, 72, 0x10);
      // int priority = bitset_shift_mask(tsbk, 72, 0x07);

      unsigned long ch1 = bitset_shift_mask(tsbk, 48, 0xffff);
      // unsigned long ch2 = bitset_shift_mask(tsbk, 32, 0xffff);
      unsigned long ga1 = bitset_shift_mask(tsbk, 16, 0xffff);
      unsigned long f1 = channel_id_to_frequency(ch1, sys_num);
      // unsigned long f2 = channel_id_to_frequency(ch2, sys_num);

      message.message_type = UPDATE;
      message.freq = f1;
      message.talkgroup = ga1;
      message.emergency = emergency;
      message.encrypted = encrypted;
      if (get_tdma_slot(ch1, sys_num) >= 0) {
        message.phase2_tdma = true;
        message.tdma_slot = get_tdma_slot(ch1, sys_num);
      } else {
        message.phase2_tdma = false;
        message.tdma_slot = 0;
      }

      os << "tsbk03\tExplicit Grant Update\tTX Channel ID: " << channel_to_string(ch1, sys_num) << "\tFreq: " << format_freq(f1) << "\tFNE TX Channel ID: " << channel_to_string(ch1, sys_num) << "\tFreq: " << format_freq(f1) << "\tga " << std::setw(7) << ga1 << "\tTDMA " << get_tdma_slot(ch1, sys_num);
      message.meta = os.str();
      BOOST_LOG_TRIVIAL(debug) << os.str();
    }
  } else if (opcode == 0x04) { //  Unit to Unit Voice Service Channel Grant (UU_V_CH_GRANT)
                               // unsigned long mfrid = bitset_shift_mask(tsbk, 80, 0xff);
    // unsigned long opts  = bitset_shift_mask(tsbk,72,0xff);
    bool emergency = (bool)bitset_shift_mask(tsbk, 72, 0x80);
    bool encrypted = (bool)bitset_shift_mask(tsbk, 72, 0x40);
    bool duplex = (bool)bitset_shift_mask(tsbk, 72, 0x20);
    bool mode = (bool)bitset_shift_mask(tsbk, 72, 0x10);
    int priority = bitset_shift_mask(tsbk, 72, 0x07);
    unsigned long ch = bitset_shift_mask(tsbk, 64, 0xffff);
    unsigned long f = channel_id_to_frequency(ch, sys_num);
    unsigned long sa = bitset_shift_mask(tsbk, 16, 0xffffff);
    unsigned long ta = bitset_shift_mask(tsbk, 40, 0xffffff);

    message.message_type = UU_V_GRANT;
    message.freq = f;
    message.talkgroup = ta;
    message.source = sa;
    message.emergency = emergency;
    message.encrypted = encrypted;
    message.duplex = duplex;
    message.mode = mode;
    message.priority = priority;
    if (get_tdma_slot(ch, sys_num) >= 0) {
      message.phase2_tdma = true;
      message.tdma_slot = get_tdma_slot(ch, sys_num);
    } else {
      message.phase2_tdma = false;
      message.tdma_slot = 0;
    }

    BOOST_LOG_TRIVIAL(debug) << "tsbk04\tUnit to Unit Chan Grant\tChannel ID: " << channel_to_string(ch, sys_num) << "\tFreq: " << format_freq(f) << "\tTarget ID: " << std::setw(7) << ta << "\tTDMA " << get_tdma_slot(ch, sys_num) << "\tSource ID: " << sa;
  } else if (opcode == 0x05) { // Unit To Unit Answer Request
    unsigned long mfrid = bitset_shift_mask(tsbk, 80, 0xff);
    if (mfrid == 0x90) { // MOTOROLA_OSP_TRAFFIC_CHANNEL_ID
      os << "MOTOROLA_OSP_TRAFFIC_CHANNEL_ID(0x05):";
      message.meta = os.str();
      BOOST_LOG_TRIVIAL(debug) << os.str();
    } else {
      bool emergency = (bool)bitset_shift_mask(tsbk, 72, 0x80);
      bool encrypted = (bool)bitset_shift_mask(tsbk, 72, 0x40);
      bool duplex = (bool)bitset_shift_mask(tsbk, 72, 0x20);
      bool mode = (bool)bitset_shift_mask(tsbk, 72, 0x10);
      int priority = bitset_shift_mask(tsbk, 72, 0x07);
      unsigned long sa = bitset_shift_mask(tsbk, 16, 0xffffff);
      unsigned long si = bitset_shift_mask(tsbk, 40, 0xffffff);

      message.message_type = UU_ANS_REQ;
      message.emergency = emergency;
      message.encrypted = encrypted;
      message.duplex = duplex;
      message.mode = mode;
      message.priority = priority;
      message.source = sa;
      message.talkgroup = si;

      BOOST_LOG_TRIVIAL(debug) << "tsbk05\tUnit To Unit Answer Request\tsa " << sa << "\tSource ID: " << si;
    }
  } else if (opcode == 0x06) { //  Unit to Unit Voice Channel Grant Update (UU_V_CH_GRANT_UPDT)
    // unsigned long mfrid = bitset_shift_mask(tsbk, 80, 0xff);
    //  unsigned long opts  = bitset_shift_mask(tsbk,72,0xff);


    unsigned long ch = bitset_shift_mask(tsbk, 64, 0xffff);
    unsigned long f = channel_id_to_frequency(ch, sys_num);
    unsigned long sa = bitset_shift_mask(tsbk, 16, 0xffffff);
    unsigned long ta = bitset_shift_mask(tsbk, 40, 0xffffff);

    message.message_type = UU_V_UPDATE;
    message.freq = f;
    message.talkgroup = ta;
    message.source = sa;
    if (get_tdma_slot(ch, sys_num) >= 0) {
      message.phase2_tdma = true;
      message.tdma_slot = get_tdma_slot(ch, sys_num);
    } else {
      message.phase2_tdma = false;
      message.tdma_slot = 0;
    }

    BOOST_LOG_TRIVIAL(debug) << "tsbk06\tUnit to Unit Chan Update\tChannel ID: " << channel_to_string(ch, sys_num) << "\tFreq: " << format_freq(f) << "\tTarget ID: " << std::setw(7) << ta << "\tTDMA " << get_tdma_slot(ch, sys_num) << "\tSource ID: " << sa;
  } else if (opcode == 0x08) {
    BOOST_LOG_TRIVIAL(debug) << "tsbk08: Telephone Interconnect Voice Channel Grant";
  } else if (opcode == 0x09) {
    unsigned long mfrid = bitset_shift_mask(tsbk, 80, 0xff);
    if (mfrid == 0x90) { // MOTOROLA_OSP_SYSTEM_LOADING
      unsigned long mk = bitset_shift_mask(tsbk, 76, 0xf);
      unsigned long ms = bitset_shift_mask(tsbk, 70, 0xff);
      unsigned long value = bitset_shift_mask(tsbk, 64, 0xffff);
      
      os << "MOTOROLA_OSP_SYSTEM_LOADING(0x09): \tScan Marker: " <<  std::dec << mk << std::setw(4) << ms << " microslots (" << std::hex << std::setfill('0') << std::setw(4) << value << ")";
      message.meta = os.str();
      BOOST_LOG_TRIVIAL(debug) << os.str();
    } else {
      BOOST_LOG_TRIVIAL(debug) << "tsbk09: Telephone Interconnect Voice Channel Grant Update";
    }
  } else if (opcode == 0x0a) {
    BOOST_LOG_TRIVIAL(debug) << "tsbk0a: Telephone Interconnect Answer Request";
  } else if (opcode == 0x14) {
    bool emergency = (bool)bitset_shift_mask(tsbk, 72, 0x80);
    bool encrypted = (bool)bitset_shift_mask(tsbk, 72, 0x40);
    bool duplex = (bool)bitset_shift_mask(tsbk, 72, 0x20);
    bool mode = (bool)bitset_shift_mask(tsbk, 72, 0x10);
    unsigned long nsapi = bitset_shift_mask(tsbk, 72, 0xf);
    unsigned long chT = bitset_shift_mask(tsbk, 56, 0xffff);
    unsigned long chR = bitset_shift_mask(tsbk, 40, 0xffff);
    unsigned long sa = bitset_shift_mask(tsbk, 16, 0xffffff);
    unsigned long fT = channel_id_to_frequency(chT, sys_num);
    unsigned long fR = channel_id_to_frequency(chR, sys_num);

    message.message_type = DATA_GRANT;
    message.emergency = emergency;
    message.encrypted = encrypted;
    message.duplex = duplex;
    message.mode = mode;
    message.source = sa;
    message.freq = fT;

    os << "sndcp_grant wuid=" << std::dec << sa
       << " chT=" << channel_to_string(chT, sys_num) << "(" << channel_id_to_freq_string(chT, sys_num) << ")"
       << " chR=" << channel_to_string(chR, sys_num) << "(" << channel_id_to_freq_string(chR, sys_num) << ")"
       << " nsapi=" << nsapi
       << " enc=" << encrypted << " dup=" << duplex;
    message.meta = os.str();
    BOOST_LOG_TRIVIAL(debug) << "tsbk14 " << os.str();
  } else if (opcode == 0x15) { // SNDCP_CH_REQ — ISP: radio requests a data session
    unsigned long svcopt = bitset_shift_mask(tsbk, 72, 0xff);
    unsigned long sa     = bitset_shift_mask(tsbk, 16, 0xffffff);

    message.source    = sa;
    message.direction = DIR_ISP;

    os << "sndcp_req wuid=" << std::dec << sa
       << " svcopt=0x" << std::hex << std::setfill('0') << std::setw(2) << svcopt;
    message.meta = os.str();
    BOOST_LOG_TRIVIAL(debug) << "tsbk15 " << os.str();
  } else if (opcode == 0x16) { // SNDCP_CH_ANNOUNCE_EXP (mfid=0x00) or MOT_UNKNOWN_16 (mfid=0x90)
    if (message.mfid == 0x00) {
      // bits[63:48] = chan_A (IDEN[3:0] + CHAN[11:0]), bits[47:32] = chan_B (same encoding, 0xffff = none)
      // bits[79:64] are options/reserved per TIA-102.AABC Table 10.16
      unsigned long chan_a = bitset_shift_mask(tsbk, 48, 0xffff);
      unsigned long chan_b = bitset_shift_mask(tsbk, 32, 0xffff);
      unsigned long fa     = channel_id_to_frequency(chan_a, sys_num);
      unsigned long fb     = channel_id_to_frequency(chan_b, sys_num);

      message.freq = fa ? fa : fb;

      os << "sndcp_announce"
         << " chA=" << channel_to_string(chan_a, sys_num)
         << "(" << channel_id_to_freq_string(chan_a, sys_num) << ")";
      if (chan_b != 0xffff) {
        os << " chB=" << channel_to_string(chan_b, sys_num)
           << "(" << channel_id_to_freq_string(chan_b, sys_num) << ")";
      }
      message.meta = os.str();
      BOOST_LOG_TRIVIAL(debug) << "tsbk16 " << os.str();
    } else if (message.mfid == 0x90) {
      // Motorola SNDCP channel announce variant — field layout not yet decoded.
      os << "mot_sndcp_announce";
      message.meta = os.str();
      // message_type stays UNKNOWN; raw_frame filled by universal auto-fill below.
      BOOST_LOG_TRIVIAL(debug) << "tsbk16 mfid=0x90 " << os.str();
    }
  } else if (opcode == 0x18) {
    BOOST_LOG_TRIVIAL(debug) << "tsbk18: Status Update";
  } else if (opcode == 0x1a) {
    BOOST_LOG_TRIVIAL(debug) << "tsbk1a: Status Query";
  } else if (opcode == 0x1c) {
    BOOST_LOG_TRIVIAL(debug) << "tsbk1c: Messag Update";
  } else if (opcode == 0x1d) {
    BOOST_LOG_TRIVIAL(debug) << "tsbk1d: Radio Unit Monitor Command";
  } else if (opcode == 0x1f) {
    BOOST_LOG_TRIVIAL(debug) << "tsbk1f: Call Alert";
  } else if (opcode == 0x20) { // Acknowledge response
    unsigned long ai_flag  = bitset_shift_mask(tsbk, 55, 0x1);
    unsigned long svc_type = bitset_shift_mask(tsbk, 48, 0x3f);
    unsigned long ga       = bitset_shift_mask(tsbk, 40, 0xffff);
    unsigned long sa       = bitset_shift_mask(tsbk, 16, 0xffffff);

    message.message_type = ACKNOWLEDGE;
    message.talkgroup = ga;
    message.source = sa;

    os << "ack_rsp wuid=" << std::dec << sa
       << " ga=" << ga
       << " svc=0x" << std::hex << std::setfill('0') << std::setw(2) << svc_type
       << " ai=" << ai_flag;
    message.meta = os.str();
    BOOST_LOG_TRIVIAL(debug) << "tsbk20 " << os.str();
  } else if (opcode == 0x21) {
    BOOST_LOG_TRIVIAL(debug) << "tsbk21: Extended Function Command";
  } else if (opcode == 0x24) {
    BOOST_LOG_TRIVIAL(debug) << "tsbk24: Extended Function Command";
  } else if (opcode == 0x27) { // Deny Response
    unsigned long reason   = bitset_shift_mask(tsbk, 72, 0xff);
    unsigned long svc_type = bitset_shift_mask(tsbk, 64, 0x3f);
    unsigned long ta       = bitset_shift_mask(tsbk, 16, 0xffffff);

    message.source = ta;

    os << "deny_rsp wuid=" << std::dec << ta
       << " reason=0x" << std::hex << std::setfill('0') << std::setw(2) << reason
       << " svc=0x" << std::setw(2) << svc_type;
    message.meta = os.str();
    BOOST_LOG_TRIVIAL(debug) << "tsbk27 " << os.str();
  } else if (opcode == 0x28) { // Unit Group Affiliation Response
    unsigned long lg  = bitset_shift_mask(tsbk, 79, 0x1);
    unsigned long gav = bitset_shift_mask(tsbk, 72, 0x3);
    unsigned long aga = bitset_shift_mask(tsbk, 56, 0xffff);
    unsigned long ga  = bitset_shift_mask(tsbk, 40, 0xffff);
    unsigned long ta  = bitset_shift_mask(tsbk, 16, 0xffffff);

    message.message_type = AFFILIATION;
    message.source = ta;
    message.talkgroup = ga;

    os << "grp_aff_rsp wuid=" << std::dec << ta
       << " ga=" << ga << " aga=" << aga
       << " gav=" << gav
       << " lg=" << lg;
    message.meta = os.str();
    BOOST_LOG_TRIVIAL(debug) << "tsbk28 " << os.str();
  } else if (opcode == 0x29) { // Secondary Control Channel Broadcast - Explicit
    unsigned long rfid = bitset_shift_mask(tsbk, 72, 0xff);
    unsigned long stid = bitset_shift_mask(tsbk, 64, 0xff);
    unsigned long ch1 = bitset_shift_mask(tsbk, 48, 0xffff);
    unsigned long ch2 = bitset_shift_mask(tsbk, 24, 0xffff);
    unsigned long f1 = channel_id_to_frequency(ch1, sys_num);
    unsigned long f2 = channel_id_to_frequency(ch2, sys_num);

    if (f1 && f2) {
      message.message_type = CONTROL_CHANNEL;
      message.freq = f1;
      message.talkgroup = 0;
      message.phase2_tdma = false;
      message.tdma_slot = 0;
      messages.push_back(message);
      message.freq = f2;

      // message.sys_id = syid;
    }
    os << "tsbk29 secondary cc: rfid " << std::dec << rfid << " stid " << stid << " ch1 " << ch1 << "(" << channel_id_to_freq_string(ch1, sys_num) << ") ch2 " << channel_to_string(ch2, sys_num) << "(" << channel_id_to_freq_string(ch2, sys_num) << ") ";

    message.meta = os.str();
    BOOST_LOG_TRIVIAL(debug) << os.str();

  } else if (opcode == 0x2a) { // Group Affiliation Query
    unsigned long ga = bitset_shift_mask(tsbk, 40, 0xffff);
    unsigned long ta = bitset_shift_mask(tsbk, 16, 0xffffff);

    message.source = ta;
    message.talkgroup = ga;

    os << "grp_aff_q wuid=" << std::dec << ta << " ga=" << ga;
    message.meta = os.str();
    BOOST_LOG_TRIVIAL(debug) << "tsbk2a " << os.str();
  } else if (opcode == 0x2b) { // Location Registration Response
    // unsigned long mfrid  = bitset_shift_mask(tsbk,80,0xff);
    unsigned long ga = bitset_shift_mask(tsbk, 56, 0xffff);
    unsigned long rv = bitset_shift_mask(tsbk, 72, 0x03);
    unsigned long sa = bitset_shift_mask(tsbk, 16, 0xffffff);

    message.message_type = LOCATION;
    message.talkgroup = ga;
    message.source = sa;

    os << "loc_reg_rsp wuid=" << std::dec << sa
       << " ga=" << ga
       << " rv=" << rv;
    message.meta = os.str();
    BOOST_LOG_TRIVIAL(debug) << "tsbk2b " << os.str();
  } else if (opcode == 0x2c) { // Unit Registration Response
    unsigned long rv   = bitset_shift_mask(tsbk, 76, 0x3);
    unsigned long syid = bitset_shift_mask(tsbk, 64, 0xfff);
    unsigned long sid  = bitset_shift_mask(tsbk, 40, 0xffffff);
    unsigned long sa   = bitset_shift_mask(tsbk, 16, 0xffffff);

    message.message_type = REGISTRATION;
    message.source = sa;

    os << "u_reg_rsp wuid=" << std::dec << sa
       << " rv=" << rv
       << " syid=0x" << std::hex << std::setfill('0') << std::setw(3) << syid
       << " sid=" << std::dec << sid;
    message.meta = os.str();
    BOOST_LOG_TRIVIAL(debug) << "tsbk2c " << os.str();
  } else if (opcode == 0x2d) { //
    BOOST_LOG_TRIVIAL(debug) << "tsbk2d AUTHENTICATION COMMAND";
  } else if (opcode == 0x2e) { //
    BOOST_LOG_TRIVIAL(debug) << "tsbk2e DE-REGISTRATION ACKNOWLEDGE";
  } else if (opcode == 0x2f) { // Unit DeRegistration Ack
    unsigned long wacn = bitset_shift_mask(tsbk, 52, 0xfffff);
    unsigned long syid = bitset_shift_mask(tsbk, 40, 0xfff);
    unsigned long sid  = bitset_shift_mask(tsbk, 16, 0xffffff);

    message.message_type = DEREGISTRATION;
    message.source = sid;

    os << "u_dereg_ack wuid=" << std::dec << sid
       << " wacn=0x" << std::hex << std::setfill('0') << std::setw(5) << wacn
       << " syid=0x" << std::setw(3) << syid;
    message.meta = os.str();
    BOOST_LOG_TRIVIAL(debug) << "tsbk2f " << os.str();
  } else if (opcode == 0x30) {
    unsigned long mfrid = bitset_shift_mask(tsbk, 80, 0xff);
    if (mfrid == 0xA4) { // GRG_EXENC_CMD (M/A-COM patch)
      // unsigned long grg_t = bitset_shift_mask(tsbk, 79, 0x1);
      unsigned long grg_g = bitset_shift_mask(tsbk, 28, 0x1);
      unsigned long grg_a = bitset_shift_mask(tsbk, 77, 0x01);
      // unsigned long grg_ssn = bitset_shift_mask(tsbk, 72, 0x1f);  //TODO: SSN should be stored and checked
      unsigned long sg = bitset_shift_mask(tsbk, 56, 0xffff);
      // unsigned long keyid = bitset_shift_mask(tsbk, 40, 0xffff);
      unsigned long rta = bitset_shift_mask(tsbk, 16, 0xffffff);
      // unsigned long algid = (rta >> 16) & 0xff;
      unsigned long ga = rta & 0xffff;
      if (grg_a == 1) {   // Activate
        if (grg_g == 1) { // Group request
          message.message_type = PATCH_ADD;
          PatchData harris_patch_data;
          harris_patch_data.sg = sg;
          harris_patch_data.ga1 = ga;
          harris_patch_data.ga2 = ga;
          harris_patch_data.ga3 = ga;
          message.patch_data = harris_patch_data;
          BOOST_LOG_TRIVIAL(debug) << "tsbk30 M/A-COM GROUP REQUEST PATCH sg TGID is " << sg << " patched with TGID " << ga;
        } else {
          message.message_type = PATCH_ADD;
          PatchData harris_patch_data;
          harris_patch_data.sg = sg;
          harris_patch_data.ga1 = ga;
          harris_patch_data.ga2 = ga;
          harris_patch_data.ga3 = ga;
          message.patch_data = harris_patch_data;
          BOOST_LOG_TRIVIAL(debug) << "tsbk30 M/A-COM UNIT REQUEST PATCH sg TGID is " << sg << " patched with TGID " << ga;
        }
      } else {            // Deactivate
        if (grg_g == 1) { // Group request
          message.message_type = PATCH_DELETE;
          PatchData harris_patch_data;
          harris_patch_data.sg = sg;
          harris_patch_data.ga1 = ga;
          harris_patch_data.ga2 = ga;
          harris_patch_data.ga3 = ga;
          message.patch_data = harris_patch_data;
          BOOST_LOG_TRIVIAL(debug) << "tsbk30 M/A-COM GROUP REQUEST PATCH DELETE for sg " << sg << " with TGID " << ga;
        } else {
          message.message_type = PATCH_DELETE;
          PatchData harris_patch_data;
          harris_patch_data.sg = sg;
          harris_patch_data.ga1 = ga;
          harris_patch_data.ga2 = ga;
          harris_patch_data.ga3 = ga;
          message.patch_data = harris_patch_data;
          BOOST_LOG_TRIVIAL(debug) << "tsbk30 M/A-COM UNIT REQUEST PATCH DELETE for sg " << sg << " with TGID " << ga;
        }
      }
    } else {
      BOOST_LOG_TRIVIAL(debug) << "tsbk30 TDMA SYNCHRONIZATION BROADCAST";
    }
  } else if (opcode == 0x31) { //
    BOOST_LOG_TRIVIAL(debug) << "tsbk31 AUTHENTICATION DEMAND";
  } else if (opcode == 0x32) { //
    BOOST_LOG_TRIVIAL(debug) << "tsbk32 AUTHENTICATION RESPONSE";
  } else if (opcode == 0x33) { // iden_up_tdma
    unsigned long mfrid = bitset_shift_mask(tsbk, 80, 0xff);

    if (mfrid == 0) {
      unsigned long iden = bitset_shift_mask(tsbk, 76, 0xf);
      unsigned long channel_type = bitset_shift_mask(tsbk, 72, 0xf);
      unsigned long toff0 = bitset_shift_mask(tsbk, 58, 0x3fff);
      unsigned long spac = bitset_shift_mask(tsbk, 48, 0x3ff);
      unsigned long toff_sign = (toff0 >> 13) & 1;
      long toff = toff0 & 0x1fff;

      if (toff_sign == 0) {
        toff = 0 - toff;
      }
      unsigned long f1 = bitset_shift_mask(tsbk, 16, 0xffffffff);
      // 16-entry table per TIA-102.AABC Table 10.82 and op25 trunking.py
      int slots_per_carrier[] = {1, 1, 1, 2, 4, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2};
      static const char *const chan_type_name[] = {
          "reserved0", "reserved1", "P25_FDMA", "P25_TDMA_2",
          "P25_TDMA_4", "P25_TDMA_2b", "tdma6", "tdma7",
          "tdma8", "tdma9", "tdma10", "tdma11",
          "tdma12", "tdma13", "tdma14", "tdma15"};
      bool chan_tdma = (slots_per_carrier[channel_type] > 1);
      Freq_Table temp_table = {
          iden,              // id;
          toff * spac * 125, // offset;
          spac * 125,        // step;
          f1 * 5,            // frequency;
          chan_tdma,
          slots_per_carrier[channel_type], // tdma;
          6.25};
      add_freq_table(iden, temp_table, sys_num);
      BOOST_LOG_TRIVIAL(debug) << "tsbk33 iden up tdma id " << std::dec << iden << " f " << temp_table.frequency << " offset " << temp_table.offset << " spacing " << temp_table.step << " slots/carrier " << temp_table.slots_per_carrier;

      std::ostringstream m33;
      m33 << std::fixed << std::setprecision(5);
      m33 << "iden=" << iden
          << " chan_type=" << chan_type_name[channel_type]
          << " slots=" << slots_per_carrier[channel_type]
          << " base_mhz=" << (temp_table.frequency / 1e6)
          << " step_khz=" << std::setprecision(3) << (temp_table.step / 1e3)
          << " txoff_mhz=" << std::showpos << (temp_table.offset / 1e6) << std::noshowpos;
      message.meta = m33.str();
    } else {
      std::ostringstream m33;
      m33 << "iden_up_tdma mfrid=0x" << std::hex << std::setfill('0') << std::setw(2) << mfrid;
      message.meta = m33.str();
      BOOST_LOG_TRIVIAL(debug) << "tsbk33 " << m33.str();
    }
  } else if (opcode == 0x34) { // iden_up vhf uhf
    unsigned long iden = bitset_shift_mask(tsbk, 76, 0xf);
    unsigned long bwvu = bitset_shift_mask(tsbk, 72, 0xf);
    unsigned long toff0 = bitset_shift_mask(tsbk, 58, 0x3fff);
    unsigned long spac = bitset_shift_mask(tsbk, 48, 0x3ff);
    unsigned long freq = bitset_shift_mask(tsbk, 16, 0xffffffff);
    unsigned long toff_sign = (toff0 >> 13) & 1;
    double bandwidth = 0;

    if (bwvu == 4) {
      bandwidth = 6.25;
    } else if (bwvu == 5) {
      bandwidth = 12.5;
    }
    long toff = toff0 & 0x1fff;

    if (toff_sign == 0) {
      toff = 0 - toff;
    }
    std::string txt[] = {"mob Tx-", "mob Tx+"};
    Freq_Table temp_table = {
        iden,              // id;
        toff * spac * 125, // offset;
        spac * 125,        // step;
        freq * 5,          // frequency;
        false,             // tdma;
        0,                 // slots
        bandwidth};
    add_freq_table(iden, temp_table, sys_num);

    BOOST_LOG_TRIVIAL(debug) << "tsbk34 iden vhf/uhf id " << std::dec << iden << " toff " << toff * spac * 0.125 * 1e-3 << " spac " << spac * 0.125 << " freq " << freq * 0.000005 << " [ " << txt[toff_sign] << "]";
  } else if (opcode == 0x35) { // Time and Date Announcement
    BOOST_LOG_TRIVIAL(debug) << "tsbk35 Time and Date Announcement";
  } else if (opcode == 0x36) { //
    BOOST_LOG_TRIVIAL(debug) << "tsbk36 ROAMING ADDRESS COMMAND";
  } else if (opcode == 0x37) { //
    BOOST_LOG_TRIVIAL(debug) << "tsbk37 ROAMING ADDRESS UPDATE";
  } else if (opcode == 0x38) { //
    BOOST_LOG_TRIVIAL(debug) << "tsbk38 SYSTEM SERVICE BROADCAST";
  } else if (opcode == 0x39) { // secondary cc
    unsigned long rfid = bitset_shift_mask(tsbk, 72, 0xff);
    unsigned long stid = bitset_shift_mask(tsbk, 64, 0xff);
    unsigned long ch1 = bitset_shift_mask(tsbk, 48, 0xffff);
    unsigned long ch2 = bitset_shift_mask(tsbk, 24, 0xffff);
    unsigned long f1 = channel_id_to_frequency(ch1, sys_num);
    unsigned long f2 = channel_id_to_frequency(ch2, sys_num);

    if (f1 && f2) {
      message.message_type = CONTROL_CHANNEL;
      message.freq = f1;
      message.talkgroup = 0;
      message.phase2_tdma = false;
      message.tdma_slot = 0;
      messages.push_back(message);
      message.freq = f2;

      // message.sys_id = syid;
    }
    os << "tsbk39 secondary cc: rfid " << std::dec << rfid << " stid " << stid << " ch1 " << channel_to_string(ch1, sys_num) << "(" << channel_id_to_freq_string(ch1, sys_num) << ") ch2 " << channel_to_string(ch2, sys_num) << "(" << channel_id_to_freq_string(ch2, sys_num) << ") ";
    message.meta = os.str();
    BOOST_LOG_TRIVIAL(debug) << os.str();
  } else if (opcode == 0x3a) { // rfss status broadcast
    unsigned long lra         = bitset_shift_mask(tsbk, 72, 0xff);
    unsigned long syid        = bitset_shift_mask(tsbk, 56, 0xfff);
    unsigned long rfid        = bitset_shift_mask(tsbk, 48, 0xff);
    unsigned long stid        = bitset_shift_mask(tsbk, 40, 0xff);
    unsigned long chan         = bitset_shift_mask(tsbk, 24, 0xffff);
    unsigned long sysservices = bitset_shift_mask(tsbk, 16, 0xff);
    unsigned long ch_iden     = (chan >> 12) & 0xf;
    unsigned long ch_no       = chan & 0xfff;
    message.message_type = SYSID;
    message.sys_id = syid;
    message.sys_rfss = rfid;
    message.sys_site_id = stid;
    os << std::hex << std::setfill('0')
       << "[LRA=0x" << std::setw(2) << lra
       << "][SysID=0x" << std::setw(3) << syid
       << "][RFSS_ID=0x" << std::setw(2) << rfid
       << "][Site_ID=0x" << std::setw(2) << stid
       << "][Ch_ID=0x" << ch_iden
       << "][Ch_No=0x" << std::setw(3) << ch_no
       << "][SysSvc=0x" << std::setw(2) << sysservices
       << "]";
    message.meta = os.str();
    BOOST_LOG_TRIVIAL(debug) << "tsbk3a " << os.str();
  } else if (opcode == 0x3b) { // network status broadcast
    unsigned long lra         = bitset_shift_mask(tsbk, 72, 0xff);
    unsigned long wacn        = bitset_shift_mask(tsbk, 52, 0xfffff);
    unsigned long syid        = bitset_shift_mask(tsbk, 40, 0xfff);
    unsigned long chan         = bitset_shift_mask(tsbk, 24, 0xffff);
    unsigned long sysservices = bitset_shift_mask(tsbk, 16, 0xff);
    unsigned long ch_iden     = (chan >> 12) & 0xf;
    unsigned long ch_no       = chan & 0xfff;
    unsigned long f1 = channel_id_to_frequency(chan, sys_num);
    if (f1) {
      message.message_type = STATUS;
      message.wacn = wacn;
      message.sys_id = syid;
      message.freq = f1;
    }
    os << std::hex << std::setfill('0')
       << "[LRA=0x" << std::setw(2) << lra
       << "][WACN=0x" << std::setw(5) << wacn
       << "][SysID=0x" << std::setw(3) << syid
       << "][Ch_ID=0x" << ch_iden
       << "][Ch_No=0x" << std::setw(3) << ch_no
       << "][SysSvc=0x" << std::setw(2) << sysservices
       << "]";
    message.meta = os.str();
    BOOST_LOG_TRIVIAL(debug) << "tsbk3b " << os.str();
  } else if (opcode == 0x3c) { // adjacent status
    unsigned long syid = bitset_shift_mask(tsbk, 60, 0xfff);
    unsigned long rfid = bitset_shift_mask(tsbk, 48, 0xff);
    unsigned long stid = bitset_shift_mask(tsbk, 40, 0xff);
    unsigned long ch1 = bitset_shift_mask(tsbk, 24, 0xffff);
    unsigned long f1 = channel_id_to_frequency(ch1, sys_num);

    if (f1) {
      it = freq_tables[stid].find((ch1 >> 12) & 0xf);
      if (it != freq_tables[stid].end()) {
        Freq_Table temp_table = it->second;
        BOOST_LOG_TRIVIAL(debug) << "\ttsbk3c Chan " << temp_table.frequency << "  " << temp_table.step;
      }
    }
    os << "adj_sts syid=0x" << std::hex << std::setfill('0') << std::setw(3) << syid
       << " rfid=" << std::dec << rfid << " stid=" << stid
       << " ch1=" << channel_to_string(ch1, sys_num) << "(" << channel_id_to_freq_string(ch1, sys_num) << ")";
    message.meta = os.str();
    BOOST_LOG_TRIVIAL(debug) << "tsbk3c " << os.str();
  } else if (opcode == 0x3d) { // iden_up
    unsigned long iden = bitset_shift_mask(tsbk, 76, 0xf);
    unsigned long bw = bitset_shift_mask(tsbk, 67, 0x1ff);
    unsigned long toff0 = bitset_shift_mask(tsbk, 58, 0x1ff);
    unsigned long spac = bitset_shift_mask(tsbk, 48, 0x3ff);
    unsigned long freq = bitset_shift_mask(tsbk, 16, 0xffffffff);
    unsigned long toff_sign = (toff0 >> 8) & 1;
    long toff = toff0 & 0xff;

    if (toff_sign == 0) {
      toff = 0 - toff;
    }

    Freq_Table temp_table = {
        iden,          // id;
        toff * 250000, // offset;
        spac * 125,    // step;
        freq * 5,      // frequency;
        false,         // tdma;
        1,             // slots
        bw * .125};
    add_freq_table(iden, temp_table, sys_num);
    BOOST_LOG_TRIVIAL(debug) << "tsbk3d iden id " << std::dec << iden << " toff " << toff * 0.25 << " spac " << spac * 0.125 << " freq " << freq * 0.000005;
  } else {
    BOOST_LOG_TRIVIAL(debug) << "tsbk_unknown: op=0x" << std::hex << opcode
                             << " mfid=0x" << message.mfid;
    // Capture the raw bytes rather than silently dropping the frame
    std::ostringstream raw;
    raw << "op=0x" << std::hex << std::setfill('0') << std::setw(2) << opcode
        << " mfid=0x" << std::setw(2) << message.mfid;
    // encode first 12 bytes of tsbk as hex
    boost::dynamic_bitset<> tmp = tsbk >> 16; // undo the pre-shift
    raw << " bytes=";
    for (int _i = 11; _i >= 0; _i--) {
      uint8_t _b = 0;
      for (int _j = 7; _j >= 0; _j--)
        _b = (_b << 1) | (unsigned int)tmp[_i * 8 + _j];
      raw << std::hex << std::setfill('0') << std::setw(2) << (unsigned int)_b;
    }
    message.raw_frame = raw.str();
    message.meta = "unknown_tsbk op=0x" + [&]{ std::ostringstream s; s << std::hex << opcode; return s.str(); }();
    messages.push_back(message);
    apply_tsbk_wrapper();
    return messages;
  }
  // Populate raw_frame for every frame that didn't already set it — covers both
  // stub branches (UNKNOWN) and fully-parsed frames (so raw bytes always appear
  // in the TSV column regardless of decode status).
  if (message.raw_frame.empty()) {
    std::ostringstream raw;
    raw << "op=0x" << std::hex << std::setfill('0') << std::setw(2) << opcode
        << " mfid=0x" << std::setw(2) << message.mfid << " bytes=";
    boost::dynamic_bitset<> tmp = tsbk >> 16;
    for (int i = 11; i >= 0; i--) {
      uint8_t b = 0;
      for (int j = 7; j >= 0; j--)
        b = (b << 1) | (unsigned int)tmp[i * 8 + j];
      raw << std::hex << std::setfill('0') << std::setw(2) << (unsigned int)b;
    }
    message.raw_frame = raw.str();
    if (message.meta.empty()) {
      std::ostringstream ms;
      ms << "stub_tsbk op=0x" << std::hex << opcode;
      message.meta = ms.str();
    }
  }
  messages.push_back(message);
  apply_tsbk_wrapper();
  return messages;
}

void P25Parser::print_bitset(boost::dynamic_bitset<> &tsbk) {
  /*boost::dynamic_bitset<> bitmask(tsbk.size(), 0x3f);
     unsigned long result = (tsbk & bitmask).to_ulong();
     BOOST_LOG_TRIVIAL(debug) << tsbk << " = " << std::hex << result;*/
}

void printbincharpad(char c) {
  for (int i = 7; i >= 0; --i) {
    std::cout << ((c & (1 << i)) ? '1' : '0');
  }

  // std::cout << " | ";
}

static std::string bytes_to_hex(const std::string &data, size_t max_len = std::string::npos) {
  std::ostringstream oss;
  oss << std::hex << std::setfill('0');
  size_t len = (max_len == std::string::npos) ? data.size() : std::min(data.size(), max_len);
  for (size_t i = 0; i < len; ++i)
    oss << std::setw(2) << (unsigned int)(uint8_t)data[i];
  return oss.str();
}

static uint16_t be16_at(const std::string &data, size_t off) {
  return ((uint16_t)(uint8_t)data[off] << 8) | (uint8_t)data[off + 1];
}

static bool ipv4_header_checksum_ok(const std::string &ip) {
  if (ip.size() < 20 || (((uint8_t)ip[0] >> 4) != 4))
    return false;
  uint8_t ihl = (uint8_t)ip[0] & 0x0f;
  size_t hdr_len = ihl * 4;
  if (ihl < 5 || hdr_len > ip.size() || (hdr_len & 1))
    return false;
  uint32_t sum = 0;
  for (size_t i = 0; i < hdr_len; i += 2)
    sum += ((uint8_t)ip[i] << 8) | (uint8_t)ip[i + 1];
  while (sum >> 16)
    sum = (sum & 0xffff) + (sum >> 16);
  return ((~sum & 0xffff) == 0);
}

// Apply fallback_freq to messages whose freq field is 0, then log.
// Used by the data-channel monitor so every frame carries the tuned channel frequency.
static void log_with_freq(std::vector<TrunkMessage> &msgs, System *sys,
                          int frame_type, double fallback_freq) {
  if (fallback_freq != 0.0) {
    for (auto &m : msgs) {
      if (m.freq == 0.0)
        m.freq = fallback_freq;
      m.recv_freq = fallback_freq;
    }
  }
  P25FrameLogger::instance().log_messages(msgs, sys, frame_type);
}

std::vector<TrunkMessage> P25Parser::parse_message(gr::message::sptr msg, System *system, double fallback_freq) {
  std::vector<TrunkMessage> messages;

  long type = msg->type();
  int sys_num = system->get_sys_num();

  if(system->has_custom_freq_table_file() && custom_freq_table_loaded == false){
    load_freq_table(system->get_custom_freq_table_file(), sys_num);
  }

  TrunkMessage message;
  message.message_type = UNKNOWN;
  message.opcode = 255;
  message.source = -1;
  message.sys_num = sys_num;
  if (type == -2) { // # request from gui
    std::string cmd = msg->to_string();

    BOOST_LOG_TRIVIAL(debug) << "process_qmsg: command: " << cmd;

    // self.update_state(cmd, curr_time)
    messages.push_back(message);
    return messages;
  } else if (type == -1) { //	# timeout

    // self.update_state('timeout', curr_time)
    messages.push_back(message);
    return messages;
  } else if (type < 0) {
    BOOST_LOG_TRIVIAL(debug) << "unknown message type " << type;
    message.message_type = INVALID_CC_MESSAGE;
    messages.push_back(message);
    return messages;
  }

  std::string s = msg->to_string();

 if (s.length() < 2) {
    if (s.length() > 0) {
      BOOST_LOG_TRIVIAL(debug) << "P25 Parse error, s: " << s << " Len: " << s.length();
    }
    message.message_type = INVALID_CC_MESSAGE;
    messages.push_back(message);
    return messages;
  }

  // # nac is always 1st two bytes
  // ac = (ord(s[0]) << 8) + ord(s[1])
  uint8_t s0 = (int)s[0];
  uint8_t s1 = (int)s[1];
  int shift = s0 << 8;
  long nac = shift + s1;

 

  if (nac == 0xffff) {
    // # TDMA
    // self.update_state('tdma_duid%d' % type, curr_time)
    messages.push_back(message);
    return messages;
  }
  s = s.substr(2);

  // Parse and strip the fixed 27-byte raw frame metadata trailer appended by op25 send_msg().
  // Format: [0xFD][raw_fs:6BE][raw_nid:8BE][bch_errors:1][crc16:2][ss_count:1][ss_dibits:8]
  uint64_t rm_raw_fs = 0, rm_raw_nid = 0;
  uint8_t  rm_bch_errors = 0, rm_ss_count = 0;
  uint16_t rm_tsbk_crc = 0;
  std::string rm_status_dibits;
  bool rm_valid = false;
  {
    const size_t TRAILER_SZ = 27;
    if (s.size() >= TRAILER_SZ && (uint8_t)s[s.size() - TRAILER_SZ] == 0xFD) {
      size_t tp = s.size() - TRAILER_SZ + 1;
      for (int i = 0; i < 6; ++i) rm_raw_fs  = (rm_raw_fs  << 8) | (uint8_t)s[tp + i]; tp += 6;
      for (int i = 0; i < 8; ++i) rm_raw_nid = (rm_raw_nid << 8) | (uint8_t)s[tp + i]; tp += 8;
      rm_bch_errors = (uint8_t)s[tp++];
      rm_tsbk_crc   = ((uint16_t)(uint8_t)s[tp] << 8) | (uint8_t)s[tp+1]; tp += 2;
      rm_ss_count   = std::min((uint8_t)s[tp++], (uint8_t)8);
      std::ostringstream ss_hex;
      ss_hex << std::hex << std::setfill('0');
      for (int i = 0; i < rm_ss_count; ++i) ss_hex << std::setw(2) << (unsigned int)(uint8_t)s[tp + i];
      rm_status_dibits = ss_hex.str();
      rm_valid = true;
      s = s.substr(0, s.size() - TRAILER_SZ);
    }
  }
  auto apply_raw_meta = [&](TrunkMessage &m) {
    if (!rm_valid) return;
    m.raw_fs = rm_raw_fs;  m.raw_nid = rm_raw_nid;  m.bch_errors = rm_bch_errors;
    m.tsbk_crc = rm_tsbk_crc;  m.ss_count = rm_ss_count;  m.status_dibits = rm_status_dibits;
  };

  BOOST_LOG_TRIVIAL(trace) << std::hex << "nac " << nac << std::dec << " type " << type << " size " << msg->to_string().length() << " mesg len: " << msg->length();
  // //" at %f state %d len %d" %(nac, type, time.time(), self.state, len(s))
  if ((type != 7) && (type != 12)) // and nac not in self.trunked_systems:
  {
    BOOST_LOG_TRIVIAL(debug) << std::hex << "NON TSBK: nac " << nac << std::dec << " type " << type << " size " << msg->to_string().length() << " mesg len: " << msg->length();
  
    /*
       if not self.configs:
     # TODO: allow whitelist/blacklist rather than blind automatic-add
        self.add_trunked_system(nac)
       else:
        return
     */
  }

  if (type == 7) { // # trunk: TSBK
    boost::dynamic_bitset<> b((s.length() + 2) * 8);

    for (unsigned int i = 0; i < s.length(); ++i) {
      unsigned char c = (unsigned char)s[i];
      b <<= 8;

      for (int j = 0; j < 8; j++) {
        if (c & 0x1) {
          b[j] = 1;
        } else {
          b[j] = 0;
        }
        c >>= 1;
      }
    }
    b <<= 16; // for missing crc

    messages = decode_tsbk(b, nac, sys_num);
    { std::string fhex = bytes_to_hex(s); for (auto &m : messages) { m.duid = 0x07; m.frame_hex = fhex; apply_raw_meta(m); } }
    log_with_freq(messages, system, 7, fallback_freq);
    return messages;
  } else if (type == 12) { // # trunk: MBT
    std::string s1 = s.substr(0, 10);
    std::string s2 = s.substr(10);
    boost::dynamic_bitset<> header((s1.length() + 2) * 8);

    for (unsigned int i = 0; i < s1.length(); ++i) {
      unsigned char c = (unsigned char)s1[i];
      header <<= 8;

      for (int j = 0; j < 8; j++) {
        if (c & 0x1) {
          header[j] = 1;
        } else {
          header[j] = 0;
        }
        c >>= 1;
      }
    }
    header <<= 16; // for missing crc

    boost::dynamic_bitset<> mbt_data((s2.length() + 4) * 8);
    for (unsigned int i = 0; i < s2.length(); ++i) {
      unsigned char c = (unsigned char)s2[i];
      mbt_data <<= 8;

      for (int j = 0; j < 8; j++) {
        if (c & 0x1) {
          mbt_data[j] = 1;
        } else {
          mbt_data[j] = 0;
        }
        c >>= 1;
      }
    }
    mbt_data <<= 32; // for missing crc
    unsigned long opcode = bitset_shift_mask(header, 32, 0x3f);
    unsigned long link_id = bitset_shift_mask(header, 48, 0xffffff);
    /*BOOST_LOG_TRIVIAL(debug) << "RAW  Data    " <<b;
    BOOST_LOG_TRIVIAL(debug) << "RAW  Data Length " <<s.length();*/
    BOOST_LOG_TRIVIAL(debug) << "MBT:  opcode: $" << std::hex << opcode;
    /* BOOST_LOG_TRIVIAL(debug) << "MBT  type :$" << std::hex << type << " len $" << std::hex << s1.length() << "/" << s2.length();
    BOOST_LOG_TRIVIAL(debug) <<  "MBT Header: " <<  header;
    BOOST_LOG_TRIVIAL(debug) <<  "MBT  Data   " <<  mbt_data; */
    messages = decode_mbt_data(opcode, header, mbt_data, link_id, nac, sys_num);
    { std::string fhex = bytes_to_hex(s); for (auto &m : messages) { m.duid = 0x0c; m.frame_hex = fhex; apply_raw_meta(m); } }
    log_with_freq(messages, system, 12, fallback_freq);
    return messages;
  } else if (type == 15) { // TDULC — Terminator Data Unit with Link Control (DUID 0x0F)
    BOOST_LOG_TRIVIAL(debug) << "P25 Parser: TDULC on control channel. Retuning to next control channel.";
    message.nac = nac;
    message.message_type = TDULC;
    message.direction = DIR_OSP;
    message.duid = 0x0f;
    if (!s.empty()) {
      std::ostringstream raw;
      raw << "tdulc bytes=";
      for (size_t i = 0; i < s.length() && i < 12; i++)
        raw << std::hex << std::setfill('0') << std::setw(2) << (unsigned int)(uint8_t)s[i];
      message.raw_frame = raw.str();
      message.meta = message.raw_frame;
    } else {
      // op25 sends type-15 as a 2-byte NAC-only notification; LC word arrives separately as type-19 LCW
      message.meta = "tdulc_event";
    }
    message.frame_hex = bytes_to_hex(s, std::min(s.size(), (size_t)12));
    apply_raw_meta(message);
    messages.push_back(message);
    log_with_freq(messages, system, 15, fallback_freq);
    return messages;
  } else if (type == 19) { // LCW or ESS (both use M_P25_FDMA_LCW)
    // ESS (Encryption Sync Sequence) from LDU2: length==12 after NAC strip
    //   mi[0..8](9) + algid(1) + keyid_hi(1) + keyid_lo(1)
    // LCW (Link Control Word) from LDU1 or TDULC: length==10 after NAC strip
    //   lcw[0..8](9) + source_duid(1)
    message.nac = nac;
    if (s.length() >= 12 && (s.length() == 12 || (uint8_t)s[12] == 0xFE)) { // ESS from LDU2 (DUID 0x0a)
      if (s.length() > 12) message.fec = s.substr(13); // fec at s[13..] when s[12]==0xFE
      uint8_t  algid = (uint8_t)s[9];
      uint16_t keyid = ((uint8_t)s[10] << 8) | (uint8_t)s[11];
      message.duid      = 0x0a;
      message.direction = DIR_OSP;
      message.opcode    = algid;   // algid as opcode — all standard algids > 0x3f,
                                   // outside valid LCCO range, so distinguishable in logger
      message.encrypted = (algid != 0x80);

      std::array<uint8_t, 9> mi_now;
      bool mi_zero = true;
      for (int i = 0; i < 9; i++) {
        mi_now[i] = (uint8_t)s[i];
        if (mi_now[i] != 0) mi_zero = false;
      }

      bool mi_changed = false;
      auto mit = last_ess_mi_.find(fallback_freq);
      if (mit == last_ess_mi_.end() || mit->second != mi_now) {
        mi_changed = true;
        last_ess_mi_[fallback_freq] = mi_now;
      }

      std::ostringstream raw;
      raw << "ess algid=0x" << std::hex << std::setfill('0') << std::setw(2) << (unsigned int)algid
          << " keyid=0x"    << std::setw(4) << keyid
          << " mi=";
      for (int i = 0; i < 9; i++)
        raw << std::setw(2) << (unsigned int)mi_now[i];
      if (mi_zero && algid != 0x80) raw << " mi_zero=1";
      if (mi_changed)               raw << " mi_changed=1";
      message.raw_frame = raw.str();
      message.meta      = message.raw_frame;
      // 12 bytes: MI(9) + algid(1) + keyid_hi(1) + keyid_lo(1); FEC marker at s[12] when present
      message.frame_hex = bytes_to_hex(s, 12);
      {
        std::ostringstream hdr;
        hdr << "{OSP:[FS=0x5575F5FF77FF][NAC=0x" << std::hex << std::setfill('0') << std::setw(3) << (unsigned long)nac
            << "][DUID=0x0a]}{ESS:[AlgID=0x" << std::setw(2) << (unsigned int)algid
            << "][KeyID=0x" << std::setw(4) << keyid
            << "]" << message.meta << "}";
        message.meta = hdr.str();
      }
    } else if (s.length() >= 9) { // LCW from LDU1 or TDULC
      if (s.length() > 10 && (uint8_t)s[10] == 0xFE) message.fec = s.substr(11);
      uint8_t source_duid = (s.length() >= 10) ? (uint8_t)s[9] : 0x05;
      uint8_t lco = (uint8_t)s[0] & 0x3f;  // Link Control Opcode
      uint8_t pb  = ((uint8_t)s[0] >> 7) & 1; // protected bit
      uint8_t sf  = ((uint8_t)s[0] >> 6) & 1; // secondary format: 0=explicit MFID, 1=abbreviated (no MFID)
      message.duid      = source_duid;
      message.direction = DIR_OSP;
      message.opcode    = lco;
      message.mfid      = (sf == 0) ? (uint8_t)s[1] : 0; // s[1] is LMC, not MFID, when SF=1
      message.encrypted = (pb == 1);

      if (pb == 0) { // only decode fields for unencrypted LCWs
        if (sf == 0) { // explicit MFID format
          if (message.mfid == 0x90) { // Motorola proprietary: +2..+4=payload, +5=FLAGS, +6..+7=payload, +8=CRC
            uint8_t  flags    = (uint8_t)s[5]; // +5 = FLAGS
            uint8_t  crc_byte = (uint8_t)s[8]; // +8 = CRC/protected
            switch (lco) {
              case 0x15: { // Motorola Call Termination — TGID at +2:+3, call_type at +4, call_handle at +7:+8
                message.talkgroup    = ((uint8_t)s[2] << 8) | (uint8_t)s[3];
                uint8_t  call_type   = (uint8_t)s[4];
                uint16_t call_handle = ((uint8_t)s[7] << 8) | (uint8_t)s[8];
                message.message_type = TDULC;
                std::ostringstream m;
                m << "mot_call_term tgid=" << std::dec << message.talkgroup
                  << " call_type=0x" << std::hex << std::setfill('0') << std::setw(2) << (unsigned)call_type
                  << " flags=0x" << std::setw(2) << (unsigned)flags
                  << " call_handle=0x" << std::setw(4) << call_handle
                  << " crc=0x" << std::setw(2) << (unsigned)crc_byte;
                message.meta = m.str();
                break;
              }
              case 0x17: { // Motorola multi-part call data (5-segment call record): +2=seq, +3:+4=payA, +5=FLAGS, +6:+7=payB, +8=CRC
                uint8_t seq_num = (uint8_t)s[2];
                std::ostringstream m;
                m << "mot_call_data seq=" << std::dec << (unsigned)seq_num
                  << " flags=0x" << std::hex << std::setfill('0') << std::setw(2) << (unsigned)flags
                  << " payload_a=" << std::setw(2) << (unsigned)(uint8_t)s[3]
                  << std::setw(2) << (unsigned)(uint8_t)s[4]
                  << " payload_b=" << std::setw(2) << (unsigned)(uint8_t)s[6]
                  << std::setw(2) << (unsigned)(uint8_t)s[7]
                  << " crc=0x" << std::setw(2) << (unsigned)crc_byte;
                message.meta = m.str();
                break;
              }
              case 0x05: { // Motorola UU_ANS_REQ: +2:+4=payA, +5=FLAGS, +6:+7=payB, +8=CRC
                std::ostringstream m;
                m << "mot_uu_ans_req"
                  << " flags=0x" << std::hex << std::setfill('0') << std::setw(2) << (unsigned)flags
                  << " payload_a=" << std::setw(2) << (unsigned)(uint8_t)s[2]
                  << std::setw(2) << (unsigned)(uint8_t)s[3]
                  << std::setw(2) << (unsigned)(uint8_t)s[4]
                  << " payload_b=" << std::setw(2) << (unsigned)(uint8_t)s[6]
                  << std::setw(2) << (unsigned)(uint8_t)s[7]
                  << " crc=0x" << std::setw(2) << (unsigned)crc_byte;
                message.meta = m.str();
                break;
              }
              default: { // Generic Motorola LCW: log payload and CRC by position
                std::ostringstream m;
                m << "mot_lcw"
                  << " flags=0x" << std::hex << std::setfill('0') << std::setw(2) << (unsigned)flags
                  << " payload=" << std::setw(2) << (unsigned)(uint8_t)s[2]
                  << std::setw(2) << (unsigned)(uint8_t)s[3]
                  << std::setw(2) << (unsigned)(uint8_t)s[4]
                  << std::setw(2) << (unsigned)(uint8_t)s[6]
                  << std::setw(2) << (unsigned)(uint8_t)s[7]
                  << " crc=0x" << std::setw(2) << (unsigned)crc_byte;
                message.meta = m.str();
                break;
              }
            }
          } else { // Standard MFID (0x00, 0x01, etc.) — TIA-102 byte layout
            if (lco == 0x00) { // Group Voice Channel User: +2=reserved, +3=svc_opts, +4:+5=TGID, +6:+8=srcaddr
              message.talkgroup    = ((uint8_t)s[4] << 8) | (uint8_t)s[5];
              message.source       = ((uint8_t)s[6] << 16) | ((uint8_t)s[7] << 8) | (uint8_t)s[8];
              message.message_type = GRANT;
            } else if (lco == 0x03) { // Unit to Unit Voice Channel User: +3:+5=srcaddr, +6:+8=dstaddr
              message.source       = ((uint8_t)s[3] << 16) | ((uint8_t)s[4] << 8) | (uint8_t)s[5];
              message.talkgroup    = ((uint8_t)s[6] << 16) | ((uint8_t)s[7] << 8) | (uint8_t)s[8];
              message.message_type = UU_V_GRANT;
            }
          }
        } else { // SF=1: abbreviated format, no MFID, s[1..8] are all payload
          if (lco == 0x02) { // Group Voice Channel Update (abbreviated)
            // Per TIA-102.AABC-C §7.9.2.1 and op25 p25p1_fdma.cc:
            // s[1:2] = 16-bit ch_A: bits[15:12]=IDEN, bits[11:0]=CHAN
            // s[3:4] = GROUP_A address (16-bit talkgroup)
            // s[5:6] = 16-bit ch_B: same encoding
            // s[7:8] = GROUP_B address (16-bit talkgroup)
            uint16_t ch_a  = ((uint8_t)s[1] << 8) | (uint8_t)s[2];
            uint16_t tg_a  = ((uint8_t)s[3] << 8) | (uint8_t)s[4];
            uint16_t ch_b  = ((uint8_t)s[5] << 8) | (uint8_t)s[6];
            uint16_t tg_b  = ((uint8_t)s[7] << 8) | (uint8_t)s[8];
            double freq_a  = channel_id_to_frequency(ch_a, sys_num) / 1e6;
            double freq_b  = channel_id_to_frequency(ch_b, sys_num) / 1e6;
            message.talkgroup    = tg_a;
            message.message_type = GRANT;
            std::ostringstream m;
            m << std::fixed << std::setprecision(4);
            m << "ch_update"
              << " chA_iden=" << ((ch_a >> 12) & 0xf)
              << " chA_num="  << (ch_a & 0xfff)
              << " chA_freq=" << freq_a
              << " tgA="      << tg_a;
            if (ch_b != ch_a || tg_b != tg_a) {
              m << " chB_iden=" << ((ch_b >> 12) & 0xf)
                << " chB_num="  << (ch_b & 0xfff)
                << " chB_freq=" << freq_b
                << " tgB="      << tg_b;
            }
            message.meta = m.str();
          } else if (lco == 0x09) { // Source ID Extension — per op25 trunking.py pb_sf_lco==0x49
            uint32_t n24 = ((uint8_t)s[2] << 16) | ((uint8_t)s[3] << 8) | (uint8_t)s[4];
            unsigned long netid = (n24 >> 4) & 0x0fffff;
            unsigned long syid  = (((uint8_t)s[4] & 0x0f) << 8) | (uint8_t)s[5];
            unsigned long sid   = ((uint8_t)s[6] << 16) | ((uint8_t)s[7] << 8) | (uint8_t)s[8];
            message.source = sid;
            std::ostringstream m;
            m << "src_id_ext wuid=" << std::dec << sid
              << " netid=0x" << std::hex << std::setfill('0') << std::setw(5) << netid
              << " syid=0x" << std::setw(3) << syid;
            message.meta = m.str();
          } else if (lco == 0x0f) { // Call Termination / Cancellation — per op25 trunking.py pb_sf_lco==0x4f
            unsigned long sa = ((uint8_t)s[6] << 16) | ((uint8_t)s[7] << 8) | (uint8_t)s[8];
            message.source = sa;
            message.message_type = TDULC;
            std::ostringstream m;
            m << "call_term_cancel wuid=" << std::dec << sa;
            message.meta = m.str();
          } else if (lco == 0x23) { // RFSS Status Broadcast (abbreviated, SF=1)
            // Per TIA-102.AABC: abbreviated form omits explicit MFID; byte layout:
            // s[1]          = LMC (Link Modification Control)
            // s[2][7:4]     = RFSS_ID[3:0]
            // s[2][3:0]+s[3] = SYS_ID[11:0]
            // s[4][7:4]     = reserved
            // s[4][3:0]     = SSN (System Status Number — 4-bit sequence counter)
            // s[5]          = SITE_ID[7:0]
            // s[6][7:4]     = CC_CHAN_ID (identifier table index)
            // s[6][3:0]+s[7] = CC_CHAN_NUM[11:0]
            // s[8]          = SYSSERVICES
            uint8_t  lmc         = (uint8_t)s[1];
            uint8_t  ssn         = (uint8_t)s[4] & 0x0f;
            message.sys_rfss    = ((uint8_t)s[2] >> 4) & 0x0f;
            message.sys_id      = (((uint8_t)s[2] & 0x0f) << 8) | (uint8_t)s[3];
            message.sys_site_id = (uint8_t)s[5];
            uint8_t  cc_iden    = ((uint8_t)s[6] >> 4) & 0x0f;
            uint16_t cc_chan    = (((uint8_t)s[6] & 0x0f) << 8) | (uint8_t)s[7];
            uint8_t  sysservices = (uint8_t)s[8];
            message.message_type = SYSID;
            std::ostringstream m;
            m << "rfss_sts"
              << " lmc=0x"       << std::hex << std::setfill('0') << std::setw(2) << (unsigned int)lmc
              << " rfss_id="     << std::dec << message.sys_rfss
              << " sys_id=0x"    << std::hex << std::setfill('0') << std::setw(3) << message.sys_id
              << " ssn="         << std::dec << (unsigned int)ssn
              << " site_id="     << message.sys_site_id
              << " cc_iden="     << (unsigned int)cc_iden
              << " cc_chan="     << cc_chan
              << " sysservices=0x" << std::hex << std::setw(2) << (unsigned int)sysservices;
            message.meta = m.str();
          }
        }
      }

      // Populate raw_frame for UNKNOWN frames; for Motorola frames with structured meta
      // already set, preserve the structured content and only add raw_frame alongside.
      if (message.message_type == UNKNOWN) {
        std::ostringstream raw;
        raw << "lcw duid=0x" << std::hex << std::setfill('0') << std::setw(2) << (unsigned int)source_duid
            << " lco=0x" << std::setw(2) << (unsigned int)lco
            << " sf=" << (unsigned int)sf
            << " pb=" << (unsigned int)pb
            << " bytes=";
        for (size_t i = 0; i < 9; i++)
          raw << std::setw(2) << (unsigned int)(uint8_t)s[i];
        message.raw_frame = raw.str();
        if (message.meta.empty()) // only use raw bytes as meta when no structured meta was set
          message.meta = message.raw_frame;
      }
      // frame_hex: 10 bytes = 9 LCW payload + source_duid (all frames, all LCOs)
      message.frame_hex = bytes_to_hex(s, std::min(s.size(), (size_t)10));
      {
        std::ostringstream hdr;
        hdr << "{OSP:[FS=0x5575F5FF77FF][NAC=0x" << std::hex << std::setfill('0') << std::setw(3) << (unsigned long)nac
            << "][DUID=0x" << std::setw(2) << (unsigned int)source_duid
            << "]}{LCW:[PB=" << std::dec << (unsigned int)pb
            << "][SF=" << (unsigned int)sf
            << "][LCO=0x" << std::hex << std::setw(2) << (unsigned int)lco;
        if (sf == 0) // explicit MFID — always include it in the header
          hdr << "][MFID=0x" << std::setw(2) << (unsigned int)message.mfid;
        if (sf == 0 && message.mfid == 0x90) // Motorola: also log the FLAGS byte (+5)
          hdr << "][FLAGS=0x" << std::setw(2) << (unsigned int)(uint8_t)s[5];
        hdr << "]" << message.meta << "}";
        message.meta = hdr.str();
      }
    }
    apply_raw_meta(message);
    messages.push_back(message);
    log_with_freq(messages, system, 19, fallback_freq);
    return messages;
  } else if (type == 18) { // Phase 2 TDMA manufacturer-specific MAC PDU
    message.nac = nac;
    if (s.length() >= 3) {
      message.opcode    = (uint8_t)s[0] & 0x3f;
      message.mfid      = (uint8_t)s[1];
      message.direction = DIR_OSP;
      message.duid      = 0xff; // Phase 2 — no standard FDMA DUID
      std::ostringstream raw;
      raw << "mac_pdu mfid=0x" << std::hex << std::setfill('0') << std::setw(2) << message.mfid
          << " op=0x" << std::setw(2) << message.opcode << " bytes=";
      for (size_t i = 0; i < s.length(); i++)
        raw << std::setw(2) << (unsigned int)(uint8_t)s[i];
      message.raw_frame = raw.str();
      message.meta      = message.raw_frame;
      message.frame_hex = bytes_to_hex(s);
    }
    message.message_type = UNKNOWN;
    apply_raw_meta(message);
    messages.push_back(message);
    log_with_freq(messages, system, 18, fallback_freq);
    return messages;
  } else if (type == 20) { // P25 PDU header (DUID 0x0C — packet data)
    message.nac       = nac;
    message.direction = DIR_OSP;
    message.duid      = 0x0c;
    message.mfid      = 0;
    std::vector<TrunkMessage> ipv4_messages;
    if (s.length() >= 3) {
      uint8_t  fmt  = (uint8_t)s[0] & 0x1f;
      uint8_t  sap  = (uint8_t)s[1] & 0x3f;
      message.opcode = fmt;

      // SAP identifier names (TIA-102.BAAA Table 9.9)
      static const char *sap_names[] = {
        "user_data","rsvd01","rsvd02","rsvd03","rsvd04","rsvd05","rsvd06","rsvd07",
        "sndcp_d",  "sndcp", "sndcp_a","sndcp_b","sndcp_c","sndcp_e","sndcp_f","sndcp_g",
      };
      const char *sap_name = (sap < 16) ? sap_names[sap] : "rsvd";

      // Extract all 12-byte PDU header fields
      uint8_t an_bit    = s.length() >= 1 ? ((uint8_t)s[0] >> 6) & 1 : 0; // acknowledged
      uint8_t io_bit    = s.length() >= 1 ? ((uint8_t)s[0] >> 5) & 1 : 0; // 1=inbound
      uint8_t mfid      = s.length() >= 3 ? (uint8_t)s[2] : 0;
      uint8_t blks      = s.length() >= 7 ? (uint8_t)s[6] & 0x7f : 0;
      uint8_t fmf_bit   = s.length() >= 7 ? ((uint8_t)s[6] >> 7) & 1 : 0; // final message fragment
      uint8_t pad_oct   = s.length() >= 8 ? ((uint8_t)s[7] >> 3) & 0x1f : 0;
      uint8_t data_off  = s.length() >= 10 ? (uint8_t)s[9] & 0x3f : 0;
      message.mfid = mfid;
      message.direction = io_bit ? DIR_ISP : DIR_OSP;

      // CRC-CCITT-16 residue check over all 12 header bytes (result==0 means CRC-OK)
      bool hdr_crc_ok = false;
      if (s.length() >= 12) {
        uint32_t poly = (1u<<12)|(1u<<5)|1u, crc = 0;
        for (int _i = 0; _i < 12; _i++) {
          uint8_t _b = (uint8_t)s[_i];
          for (int _j = 7; _j >= 0; _j--) {
            crc = ((crc << 1) | ((_b >> _j) & 1)) & 0x1ffff;
            if (crc & 0x10000) crc = (crc & 0xffff) ^ poly;
          }
        }
        hdr_crc_ok = ((crc ^ 0xffff) & 0xffff) == 0;
      }

      std::ostringstream meta;
      meta << "pdu fmt=0x" << std::hex << std::setfill('0') << std::setw(2) << (unsigned)fmt
           << " sap=" << sap_name << "(0x" << std::setw(2) << (unsigned)sap << ")"
           << " mfid=0x" << std::setw(2) << (unsigned)mfid;

      if (s.length() >= 6) {
        uint32_t dst = ((uint8_t)s[3] << 16) | ((uint8_t)s[4] << 8) | (uint8_t)s[5];
        message.source = dst;
        meta << " dst=" << std::dec << dst;
      }
      meta << std::dec
           << " blks=" << (unsigned)blks
           << " an=" << (unsigned)an_bit
           << " io=" << (unsigned)io_bit
           << " data_off=" << (unsigned)data_off
           << " pad=" << (unsigned)pad_oct
           << " hdr_crc=" << (hdr_crc_ok ? "ok" : "BAD");
      if (fmf_bit) meta << " last";

      // Scan backward for 0xAB 0xCD fec section marker; exclude from payload parsing
      // and later hex dump when found.
      size_t fec_start = std::string::npos;
      if (s.length() >= 14) {
        for (size_t i = s.length() - 2; i >= 12; --i) {
          if ((uint8_t)s[i] == 0xAB && (uint8_t)s[i+1] == 0xCD) {
            bool valid = true;
            for (size_t j = i+2; j < s.length(); ++j)
              if ((uint8_t)s[j] < 0x20 || (uint8_t)s[j] >= 0x80) { valid = false; break; }
            if (valid) { fec_start = i; break; }
          }
          if (i == 0) break;
        }
      }
      size_t hex_end = (fec_start != std::string::npos) ? fec_start : s.length();

      // Assemble confirmed data payload before parsing SNDCP/IP. Each decoded
      // 18-byte confirmed data block begins with DBSN/CRC9 overhead, followed by
      // 16 bytes of user payload. The PDU header data_off value is relative to the
      // raw first block; after stripping the first block's 2-byte overhead, reduce
      // the offset by two. Parsing the raw 18-byte stream crosses block overhead
      // bytes and produces false IPv4 checksum failures.
      bool confirmed_data = (an_bit != 0) && (fmt == 0x16);
      size_t captured_blks = 0;
      if (hex_end >= 12)
        captured_blks = (hex_end - 12) / 18;
      if (blks != 0 && captured_blks > blks)
        captured_blks = blks;

      std::string pdu_payload;
      for (size_t bi = 0; bi < captured_blks; ++bi) {
        size_t block_off = 12 + bi * 18;
        if (block_off + 18 > hex_end)
          break;
        if (confirmed_data) {
          pdu_payload.append(s.data() + block_off + 2, 16);
        } else {
          pdu_payload.append(s.data() + block_off, 18);
        }
      }

      size_t payload_off = data_off;
      if (confirmed_data)
        payload_off = (data_off > 2) ? (data_off - 2) : 0;

      size_t payload_len = pdu_payload.size();
      if (pad_oct > 0 && pad_oct < payload_len)
        payload_len -= pad_oct;

      meta << " cap_blks=" << captured_blks
           << " payload_mode=" << (confirmed_data ? "strip_dbh_adjusted" : "raw");
      if (blks != captured_blks)
        meta << " pdu_trunc=1";

      bool have_sndcp = false;
      uint8_t sndcp_x_val = 0;
      uint8_t sndcp_t_val = 0;
      uint8_t sndcp_m_val = 0;
      uint8_t sndcp_nsapi_val = 0;
      uint8_t sndcp_npdu_seq_val = 0;
      std::string sndcp_ip_segment;

      if (payload_off + 1 < payload_len) {
        const uint8_t *payload = (const uint8_t*)pdu_payload.data();
        uint8_t sc0 = payload[payload_off];
        uint8_t sc1 = payload[payload_off + 1];
        uint8_t sndcp_x    = (sc0 >> 7) & 1;    // 1 = compressed header
        uint8_t sndcp_t    = (sc0 >> 6) & 1;    // 1 = data PDU (SN-DATA)
        uint8_t sndcp_m    = (sc0 >> 5) & 1;    // 1 = more segments follow
        uint8_t sndcp_nsapi = sc0 & 0x1f;
        have_sndcp = true;
        sndcp_x_val = sndcp_x;
        sndcp_t_val = sndcp_t;
        sndcp_m_val = sndcp_m;
        sndcp_nsapi_val = sndcp_nsapi;
        sndcp_npdu_seq_val = sc1;
        size_t ip_payload_off = payload_off + 2;
        meta << " sndcp_x=" << (unsigned)sndcp_x
             << " sndcp_t=" << (unsigned)sndcp_t
             << " sndcp_m=" << (unsigned)sndcp_m
             << " nsapi=" << (unsigned)sndcp_nsapi
             << " npdu_seq=" << (unsigned)sc1
             << " ip_payload_off=" << ip_payload_off;
        // Peek at potential IPv4 header byte to aid verification
        if (ip_payload_off < payload_len) {
          const uint8_t *ip = payload + ip_payload_off;
          size_t ip_avail = payload_len - ip_payload_off;
          sndcp_ip_segment.assign((const char*)ip, ip_avail);
          uint8_t ip0 = ip[0];
          meta << " ip0=0x" << std::hex << std::setfill('0') << std::setw(2) << (unsigned)ip0;
          if ((ip0 >> 4) == 4) {
            // Log IP total_len and protocol for IPv4
            if (ip_avail > 9) {
              uint16_t ip_len  = (ip[2] << 8) | ip[3];
              uint16_t ip_id   = (ip[4] << 8) | ip[5];
              uint8_t  ip_ttl  = ip[8];
              uint8_t  ip_proto= ip[9];
              meta << std::dec
                   << " ip_len=" << ip_len
                   << " ip_id=" << ip_id
                   << " ip_ttl=" << (unsigned)ip_ttl
                   << " ip_proto=" << (unsigned)ip_proto;
            }
            if (ip_avail >= 20) {
              meta << " ip_src=" << std::dec
                   << (unsigned)ip[12] << "." << (unsigned)ip[13] << "."
                   << (unsigned)ip[14] << "." << (unsigned)ip[15]
                   << " ip_dst="
                   << (unsigned)ip[16] << "." << (unsigned)ip[17] << "."
                   << (unsigned)ip[18] << "." << (unsigned)ip[19];
            }
            // Verify IPv4 header checksum over 20 bytes when available
            if (ip_avail >= 20) {
              uint32_t sum = 0;
              for (size_t _w = 0; _w < 20; _w += 2)
                sum += (ip[_w] << 8) | ip[_w+1];
              while (sum >> 16) sum = (sum & 0xffff) + (sum >> 16);
              meta << " ip_crc=" << ((~sum & 0xffff) == 0 ? "ok" : "BAD");
            }
          }
        }
      }
      if (fec_start != std::string::npos) {
        std::string fec_full = s.substr(fec_start + 2);
        auto rawbits_pos = fec_full.find("|RAWBITS:");
        std::string fec_meta;
        if (rawbits_pos != std::string::npos) {
          message.pre_fec_bits = fec_full.substr(rawbits_pos + 9);
          message.fec          = fec_full.substr(0, rawbits_pos);
          fec_meta             = message.fec;
        } else {
          message.fec = fec_full;
          fec_meta    = fec_full;
        }

        auto fec_field = [&fec_meta](const std::string& key) -> std::string {
          std::string needle = "|" + key + ":";
          size_t pos = fec_meta.find(needle);
          size_t val = std::string::npos;
          if (pos != std::string::npos) {
            val = pos + needle.size();
          } else if (fec_meta.rfind(key + ":", 0) == 0) {
            val = key.size() + 1;
          }
          if (val == std::string::npos)
            return std::string();
          size_t end = fec_meta.find('|', val);
          return fec_meta.substr(val, end == std::string::npos ? std::string::npos : end - val);
        };
        std::string seq = fec_field("SEQ");
        std::string frlen = fec_field("FRLEN");
        std::string bv34 = fec_field("BV34");
        std::string capblks = fec_field("CAPBLKS");
        std::string phyblks = fec_field("PHYBLKS");
        std::string extrablks = fec_field("EXTRABLKS");
        std::string allrawbits = fec_field("ALLRAWBITS");
        std::string extradata = fec_field("EXTRADATA");
        if (!seq.empty()) meta << " frame_seq=" << seq;
        if (!frlen.empty()) meta << " fr_len=" << frlen;
        if (!bv34.empty()) meta << " bv34=" << bv34;
        if (!capblks.empty()) meta << " decoder_cap_blks=" << capblks;
        if (!phyblks.empty()) meta << " phy_blks=" << phyblks;
        if (!extrablks.empty()) meta << " extra_blks=" << extrablks;
        if (!allrawbits.empty()) meta << std::dec << " allrawbits_hex_len=" << allrawbits.size();
        if (!extradata.empty()) meta << std::dec << " extradata_hex_len=" << extradata.size();
        if (!message.pre_fec_bits.empty()) meta << std::dec << " rawbits_hex_len=" << message.pre_fec_bits.size();
      }
      std::ostringstream raw;
      raw << meta.str() << " bytes=";
      for (size_t i = 0; i < hex_end; i++)
        raw << std::hex << std::setfill('0') << std::setw(2) << (unsigned int)(uint8_t)s[i];
      message.raw_frame = raw.str();
      message.meta      = meta.str();
      message.frame_hex = bytes_to_hex(s, hex_end);

      auto emit_ipv4_packet = [&](const std::string &ip_bytes,
                                  const std::vector<std::string> &raw_frames,
                                  unsigned int segments,
                                  bool reassembled) -> bool {
        if (ip_bytes.size() < 20 || (((uint8_t)ip_bytes[0] >> 4) != 4))
          return false;
        uint8_t ihl = (uint8_t)ip_bytes[0] & 0x0f;
        size_t hdr_len = ihl * 4;
        if (ihl < 5 || hdr_len > ip_bytes.size())
          return false;
        uint16_t ip_len = be16_at(ip_bytes, 2);
        if (ip_len < hdr_len || ip_len > ip_bytes.size())
          return false;
        std::string packet = ip_bytes.substr(0, ip_len);
        if (!ipv4_header_checksum_ok(packet))
          return false;

        TrunkMessage out = message;
        out.message_type = UNKNOWN;
        out.duid = 0x0c;
        out.fec.clear();
        out.pre_fec_bits.clear();
        out.raw_fs = 0;
        out.raw_nid = 0;
        out.bch_errors = 0;
        out.tsbk_crc = 0;
        out.ss_count = 0;
        out.status_dibits.clear();
        out.frame_hex = bytes_to_hex(packet);

        uint16_t ip_id = be16_at(packet, 4);
        uint8_t ip_proto = (uint8_t)packet[9];
        std::ostringstream ip_src;
        ip_src << std::dec << (unsigned)(uint8_t)packet[12] << "."
               << (unsigned)(uint8_t)packet[13] << "."
               << (unsigned)(uint8_t)packet[14] << "."
               << (unsigned)(uint8_t)packet[15];
        std::ostringstream ip_dst;
        ip_dst << std::dec << (unsigned)(uint8_t)packet[16] << "."
               << (unsigned)(uint8_t)packet[17] << "."
               << (unsigned)(uint8_t)packet[18] << "."
               << (unsigned)(uint8_t)packet[19];

        std::ostringstream out_meta;
        out_meta << "ipv4_packet=1"
                 << " reassembled=" << (reassembled ? 1 : 0)
                 << " segments=" << segments
                 << " llid=" << message.source
                 << " fmt=0x" << std::hex << std::setfill('0') << std::setw(2) << (unsigned)fmt
                 << " sap=0x" << std::setw(2) << (unsigned)sap
                 << std::dec
                 << " nsapi=" << (unsigned)sndcp_nsapi_val
                 << " npdu_seq=" << (unsigned)sndcp_npdu_seq_val
                 << " ip_len=" << ip_len
                 << " ip_id=" << ip_id
                 << " ip_proto=" << (unsigned)ip_proto
                 << " ip_src=" << ip_src.str()
                 << " ip_dst=" << ip_dst.str()
                 << " ip_crc=ok"
                 << " raw_pdu_segments=" << raw_frames.size();
        out.meta = out_meta.str();

        std::ostringstream out_raw;
        out_raw << "ipv4_packet nbytes=" << packet.size()
                << " reassembled=" << (reassembled ? 1 : 0)
                << " segments=" << segments
                << " raw_pdu_frame_hexes=";
        for (size_t i = 0; i < raw_frames.size(); ++i) {
          if (i) out_raw << "|";
          out_raw << raw_frames[i];
        }
        out_raw << " ipv4=" << out.frame_hex;
        out.raw_frame = out_raw.str();
        ipv4_messages.push_back(out);
        return true;
      };

      if (have_sndcp && sndcp_t_val == 1 && sndcp_x_val == 0 && !sndcp_ip_segment.empty()) {
        std::ostringstream key;
        key << nac << "|" << (unsigned)message.direction << "|" << (uint64_t)fallback_freq
            << "|" << message.source << "|" << (unsigned)sap
            << "|" << (unsigned)sndcp_nsapi_val << "|" << (unsigned)sndcp_npdu_seq_val;
        std::string key_s = key.str();
        bool starts_ipv4 = (((uint8_t)sndcp_ip_segment[0] >> 4) == 4);

        if (sndcp_m_val) {
          auto &state = sndcp_reassembly_[key_s];
          if (starts_ipv4 || state.segments == 0) {
            state = SndcpReassemblyState{};
            state.base = message;
          }
          state.ipv4_bytes.append(sndcp_ip_segment);
          state.raw_frames.push_back(message.frame_hex);
          state.segments++;
        } else {
          auto it_reasm = sndcp_reassembly_.find(key_s);
          if (it_reasm != sndcp_reassembly_.end()) {
            it_reasm->second.ipv4_bytes.append(sndcp_ip_segment);
            it_reasm->second.raw_frames.push_back(message.frame_hex);
            it_reasm->second.segments++;
            emit_ipv4_packet(it_reasm->second.ipv4_bytes,
                             it_reasm->second.raw_frames,
                             it_reasm->second.segments,
                             true);
            sndcp_reassembly_.erase(it_reasm);
          } else if (starts_ipv4) {
            std::vector<std::string> raw_frames;
            raw_frames.push_back(message.frame_hex);
            emit_ipv4_packet(sndcp_ip_segment, raw_frames, 1, false);
          }
        }
      }
    }
    message.message_type = UNKNOWN;
    apply_raw_meta(message);
    messages.push_back(message);
    log_with_freq(messages, system, 20, fallback_freq);
    if (!ipv4_messages.empty())
      log_with_freq(ipv4_messages, system, 23, fallback_freq);
    return messages;
  } else if (type == 22) { // HDU — Header Data Unit, call start (DUID 0x00)
    // payload: MI(9) + MFID(1) + algid(1) + keyid(2) + tgid(2) = 15 bytes after NAC strip
    message.nac = nac;
    if (s.length() >= 15) {
      if (s.length() > 15 && (uint8_t)s[15] == 0xFE) message.fec = s.substr(16);
      uint8_t  mfid  = (uint8_t)s[9];
      uint8_t  algid = (uint8_t)s[10];
      uint16_t keyid = ((uint8_t)s[11] << 8) | (uint8_t)s[12];
      uint16_t tgid  = ((uint8_t)s[13] << 8) | (uint8_t)s[14];
      message.duid      = 0x00;
      message.direction = DIR_OSP;
      message.opcode    = algid;   // algid — all standard P25 algids > 0x3f
      message.mfid      = mfid;
      message.talkgroup = tgid;
      message.encrypted = (algid != 0x80);

      bool mi_zero = true;
      for (int i = 0; i < 9; i++)
        if ((uint8_t)s[i] != 0) { mi_zero = false; break; }

      std::ostringstream raw;
      raw << "hdu algid=0x" << std::hex << std::setfill('0') << std::setw(2) << (unsigned int)algid
          << " keyid=0x"    << std::setw(4) << keyid
          << " tgid="       << std::dec    << tgid
          << " mi=";
      for (int i = 0; i < 9; i++)
        raw << std::hex << std::setfill('0') << std::setw(2) << (unsigned int)(uint8_t)s[i];
      if (mi_zero && algid != 0x80) raw << " mi_zero=1";
      message.raw_frame = raw.str();
      message.meta      = message.raw_frame;
      // 15 bytes: MI(9)+MFID(1)+algid(1)+keyid(2)+tgid(2); FEC marker at s[15] when present
      message.frame_hex = bytes_to_hex(s, 15);
      {
        std::ostringstream hdr;
        hdr << "{OSP:[FS=0x5575F5FF77FF][NAC=0x" << std::hex << std::setfill('0') << std::setw(3) << (unsigned long)nac
            << "][DUID=0x00]}{HDU:[MFID=0x" << std::setw(2) << (unsigned int)mfid
            << "][AlgID=0x" << std::setw(2) << (unsigned int)algid
            << "][KeyID=0x" << std::setw(4) << keyid
            << "][TGID=" << std::dec << tgid
            << "]" << message.meta << "}";
        message.meta = hdr.str();
      }
    }
    message.message_type = UNKNOWN;
    apply_raw_meta(message);
    messages.push_back(message);
    log_with_freq(messages, system, 22, fallback_freq);
    return messages;
  } else if (type == 21) { // M_P25_RAW_FRAME — raw frame bits (post-sync, pre-FEC) for all DUIDs
    // Payload: byte[0]=actual DUID, bytes[1..]=packed bits from bit-48 onwards (NID+body).
    // Primary use: log LDU1/LDU2/HDU/TDU bytes that have no other raw-byte log path,
    // and to provide a complete pre-FEC byte record for every uplink frame.
    static const char *duid_names[] = {
      "HDU",nullptr,nullptr,"TDU",nullptr,"LDU1",nullptr,"TSBK",
      nullptr,nullptr,"LDU2",nullptr,"PDU",nullptr,nullptr,"TDULC"
    };
    message.nac       = nac;
    message.direction = DIR_ISP; // all RAW_FRAME captures come from the uplink receiver
    if (!s.empty()) {
      uint8_t actual_duid = (uint8_t)s[0];
      const char *dname = (actual_duid < 16 && duid_names[actual_duid]) ? duid_names[actual_duid] : "UNK";
      message.duid         = actual_duid;
      message.message_type = UNKNOWN;

      size_t raw_end = s.length();
      std::string raw_meta_extra;
      if (s.length() >= 4) {
        for (size_t i = s.length() - 2; i >= 1; --i) {
          if ((uint8_t)s[i] == 0xAB && (uint8_t)s[i+1] == 0xCE) {
            bool valid = true;
            for (size_t j = i + 2; j < s.length(); ++j) {
              uint8_t c = (uint8_t)s[j];
              if (c < 0x20 || c >= 0x80) { valid = false; break; }
            }
            if (valid) {
              raw_end = i;
              raw_meta_extra = s.substr(i + 2);
              break;
            }
          }
          if (i == 1) break;
        }
      }

      std::ostringstream raw;
      raw << "raw_frame duid=0x" << std::hex << std::setfill('0') << std::setw(2) << (unsigned)actual_duid
          << "(" << dname << ") nac=0x" << std::setw(3) << (unsigned)nac;
      if (!raw_meta_extra.empty())
        raw << " " << raw_meta_extra;

      // LDU1 (0x05) and LDU2 (0x0a): AES-256 encrypted voice; extract Low Speed Data.
      // Payload layout (s[0]=duid, s[1..210]=packed frame bits 48-1727 MSB-first):
      //   s[1..2]    = NID bytes 0-1 → NAC[11:4]=s[1], NAC[3:0]=s[2]>>4, DUID=s[2]&0x0f
      //   s[1..9]    = NID (64 data bits including BCH parity, interleaved with status dibits)
      //   s[9..152]  = 9×144-bit IMBE codewords (voice_codeword_bits[0..8], scattered via
      //                interleaving table; AES-256 cipher occupies u0-u7 of each codeword)
      //   s[153..187]= LCW/ESS overhead: 24×Hamming(10,6) codewords = 240 bits
      //   s[188..192]= LSD (Low Speed Data): 32 bits at frame positions 1546-1577
      //                  LSD1[15:0] = frame bits 1546-1561
      //                  LSD2[15:0] = frame bits 1562-1577
      if ((actual_duid == 0x05 || actual_duid == 0x0a) && raw_end >= 193) {
        message.encrypted = true;
        // Frame bit b → payload byte (b-48)/8, MSB-first within byte.
        // LSD1: bits 1546-1561 → bytes 187-189 (s[188]-s[190]), crossing two byte boundaries.
        // LSD2: bits 1562-1577 → bytes 189-191 (s[190]-s[192]).
        uint16_t lsd1 = (((uint8_t)s[188] & 0x3F) << 10) | ((uint8_t)s[189] << 2) | ((uint8_t)s[190] >> 6);
        uint16_t lsd2 = (((uint8_t)s[190] & 0x3F) << 10) | ((uint8_t)s[191] << 2) | ((uint8_t)s[192] >> 6);
        raw << " enc=AES256 lsd1=0x" << std::hex << std::setfill('0') << std::setw(4) << lsd1
            << " lsd2=0x" << std::setw(4) << lsd2;
      }

      raw << " nbytes=" << std::dec << (raw_end > 0 ? raw_end - 1 : 0) << " hex=";
      for (size_t i = 1; i < raw_end; i++)
        raw << std::hex << std::setfill('0') << std::setw(2) << (unsigned)(uint8_t)s[i];
      message.raw_frame = raw.str();
      message.meta      = message.raw_frame;
      message.frame_hex = bytes_to_hex(s.substr(1, raw_end > 0 ? raw_end - 1 : 0));
    }
    apply_raw_meta(message);
    messages.push_back(message);
    log_with_freq(messages, system, 21, fallback_freq);
    return messages;
  }
  messages.push_back(message);
  return messages;
}