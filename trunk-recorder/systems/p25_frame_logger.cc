#include "p25_frame_logger.h"
#include "system.h"
#include <chrono>
#include <ctime>
#include <iomanip>
#include <sstream>

P25FrameLogger &P25FrameLogger::instance() {
  static P25FrameLogger inst;
  return inst;
}

P25FrameLogger::~P25FrameLogger() {
  close();
}

void P25FrameLogger::open(const std::string &path) {
  std::lock_guard<std::mutex> lock(mtx_);
  log_file_.open(path, std::ios::app);
  if (log_file_.is_open()) {
    // Only write header if file is new/empty
    log_file_.seekp(0, std::ios::end);
    if (log_file_.tellp() == 0) {
      write_header();
    }
  }
}

void P25FrameLogger::close() {
  std::lock_guard<std::mutex> lock(mtx_);
  if (log_file_.is_open())
    log_file_.close();
}

bool P25FrameLogger::is_open() const {
  std::lock_guard<std::mutex> lock(mtx_);
  return log_file_.is_open();
}

void P25FrameLogger::write_header() {
  log_file_ << "timestamp\tsys_name\tnac\tdirection\tframe_type\tmfid\topcode_hex\t"
               "opcode_name\tdecode_status\ttalkgroup\tsource_id\tfreq_mhz\t"
               "emergency\tencrypted\tphase2_tdma\ttdma_slot\twacn\tsys_id\t"
               "rfss_id\tsite_id\traw_frame\tmeta\n";
  log_file_.flush();
}

std::string P25FrameLogger::ts_now() const {
  using namespace std::chrono;
  auto now   = system_clock::now();
  auto ms    = duration_cast<milliseconds>(now.time_since_epoch()) % 1000;
  auto t     = system_clock::to_time_t(now);
  std::tm tm_utc{};
  gmtime_r(&t, &tm_utc);
  char buf[32];
  strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%S", &tm_utc);
  std::ostringstream oss;
  oss << buf << '.' << std::setfill('0') << std::setw(3) << ms.count() << 'Z';
  return oss.str();
}

std::string P25FrameLogger::decode_status(const TrunkMessage &msg) const {
  if (msg.message_type == UNKNOWN)
    return "RAW";
  if (!msg.raw_frame.empty())
    return "PARTIAL";
  return "FULL";
}

std::string P25FrameLogger::opcode_name(unsigned long opcode, unsigned long mfid, int frame_type) const {
  if (frame_type == 12) { // MBT
    if (mfid == 0x90) {
      switch (opcode) {
        case 0x02: return "MBT_MOT_GRG_CN_GRANT_EXP";
        default:   break;
      }
    }
    switch (opcode) {
      case 0x00: return "MBT_GRP_V_CH_GRANT";
      case 0x04: return "MBT_UU_V_CH_GRANT";
      case 0x28: return "MBT_GRP_AFF_RSP";
      default:   return "MBT_UNKNOWN";
    }
  }

  // TSBK / Phase2 abbreviated (frame_type 7 or 20 raw PDU)
  if (mfid == 0x90) {
    switch (opcode) {
      case 0x00: return "TSBK_MOT_GRG_ADD_CMD";
      case 0x01: return "TSBK_MOT_GRG_DEL_CMD";
      case 0x02: return "TSBK_MOT_OSP_PATCH_GRP_CH_GRANT";
      case 0x03: return "TSBK_MOT_OSP_PATCH_GRP_CH_GRANT_UPDT";
      case 0x05: return "TSBK_MOT_OSP_TRAFFIC_CH_ID";
      case 0x06: return "TSBK_MOT_GRG_CN_GRANT_UPDT";
      case 0x09: return "TSBK_MOT_OSP_SYSTEM_LOADING";
      default:   return "TSBK_MOT_UNKNOWN";
    }
  }
  if (mfid == 0xA4) {
    switch (opcode) {
      case 0x30: return "TSBK_MACOM_GRG_EXENC_CMD";
      default:   return "TSBK_MACOM_UNKNOWN";
    }
  }

  // Standard APCO opcodes
  switch (opcode) {
    case 0x00: return "TSBK_GRP_V_CH_GRANT";
    case 0x01: return "TSBK_RESERVED_01";
    case 0x02: return "TSBK_GRP_V_CH_GRANT_UPDT";
    case 0x03: return "TSBK_GRP_V_CH_GRANT_UPDT_EXP";
    case 0x04: return "TSBK_UU_V_CH_GRANT";
    case 0x05: return "TSBK_UU_ANS_REQ";
    case 0x06: return "TSBK_UU_V_CH_GRANT_UPDT";
    case 0x08: return "TSBK_TELEPHONE_INT_V_CH_GRANT";
    case 0x09: return "TSBK_TELEPHONE_INT_V_CH_GRANT_UPDT";
    case 0x0a: return "TSBK_TELEPHONE_INT_ANS_REQ";
    case 0x14: return "TSBK_SNDCP_CH_GRANT";
    case 0x15: return "TSBK_SNDCP_CH_REQ";          // ISP
    case 0x16: return "TSBK_SNDCP_CH_ANNOUNCE_EXP";
    case 0x18: return "TSBK_STATUS_UPDATE";
    case 0x1a: return "TSBK_STATUS_QUERY";
    case 0x1c: return "TSBK_MSG_UPDATE";
    case 0x1d: return "TSBK_RADIO_UNIT_MONITOR_CMD";
    case 0x1f: return "TSBK_CALL_ALERT";
    case 0x20: return "TSBK_ACK_RSP_FNE";
    case 0x21: return "TSBK_EXT_FUNC_CMD";
    case 0x24: return "TSBK_EXT_FUNC_CMD_ALT";
    case 0x27: return "TSBK_DENY_RSP";
    case 0x28: return "TSBK_GRP_AFF_RSP";
    case 0x29: return "TSBK_SCCB_EXP";
    case 0x2a: return "TSBK_GRP_AFF_Q";
    case 0x2b: return "TSBK_LOC_REG_RSP";
    case 0x2c: return "TSBK_U_REG_RSP";
    case 0x2d: return "TSBK_AUTH_CMD";
    case 0x2e: return "TSBK_U_DE_REG_ACK";
    case 0x2f: return "TSBK_GRP_V_CH_GRANT_UPDT_MBT"; // Also UU DeReg Ack
    case 0x30: return "TSBK_TDMA_SYNC_BCAST";
    case 0x31: return "TSBK_AUTH_DEMAND";
    case 0x32: return "TSBK_AUTH_RSP";
    case 0x33: return "TSBK_IDEN_UP_TDMA";
    case 0x34: return "TSBK_IDEN_UP_VU";
    case 0x35: return "TSBK_TIME_DATE_ANNOUNCE";
    case 0x36: return "TSBK_ROAMING_ADDR_CMD";
    case 0x37: return "TSBK_ROAMING_ADDR_UPDATE";
    case 0x38: return "TSBK_SYS_SVC_BCAST";
    case 0x39: return "TSBK_SCCB";
    case 0x3a: return "TSBK_RFSS_STS_BCAST";
    case 0x3b: return "TSBK_NET_STS_BCAST";
    case 0x3c: return "TSBK_ADJ_STS_BCAST";
    case 0x3d: return "TSBK_IDEN_UP";
    case 0x3e: return "TSBK_PROTECTED_SITE_DATA";
    case 0x3f: return "TSBK_RESERVED_3F";
    default:   return "TSBK_UNKNOWN";
  }
}

std::string P25FrameLogger::format_record(const TrunkMessage &msg, System *system, int frame_type) const {
  std::string frame_type_str;
  switch (frame_type) {
    case 7:  frame_type_str = "TSBK";    break;
    case 12: frame_type_str = "MBT";     break;
    case 18: frame_type_str = "MAC_PDU"; break;
    case 20: frame_type_str = "RAW_PDU"; break;
    default: frame_type_str = std::to_string(frame_type); break;
  }

  std::string dir_str;
  switch (msg.direction) {
    case DIR_OSP:     dir_str = "OSP"; break;
    case DIR_ISP:     dir_str = "ISP"; break;
    default:          dir_str = "UNK"; break;
  }

  std::ostringstream opcode_hex;
  opcode_hex << "0x" << std::hex << std::setfill('0') << std::setw(2) << msg.opcode;

  std::ostringstream mfid_hex;
  mfid_hex << "0x" << std::hex << std::setfill('0') << std::setw(2) << msg.mfid;

  std::ostringstream nac_hex;
  nac_hex << "0x" << std::hex << std::setfill('0') << std::setw(3) << msg.nac;

  std::ostringstream freq_str;
  if (msg.freq > 0)
    freq_str << std::fixed << std::setprecision(4) << (msg.freq / 1e6);
  else
    freq_str << "0";

  std::string sys_name = system ? system->get_short_name() : "unknown";

  std::ostringstream rec;
  rec << ts_now()              << '\t'
      << sys_name              << '\t'
      << nac_hex.str()         << '\t'
      << dir_str               << '\t'
      << frame_type_str        << '\t'
      << mfid_hex.str()        << '\t'
      << opcode_hex.str()      << '\t'
      << opcode_name(msg.opcode, msg.mfid, frame_type) << '\t'
      << decode_status(msg)    << '\t'
      << msg.talkgroup         << '\t'
      << msg.source            << '\t'
      << freq_str.str()        << '\t'
      << (msg.emergency ? 1 : 0) << '\t'
      << (msg.encrypted ? 1 : 0) << '\t'
      << (msg.phase2_tdma ? 1 : 0) << '\t'
      << msg.tdma_slot         << '\t'
      << msg.wacn              << '\t'
      << msg.sys_id            << '\t'
      << msg.sys_rfss          << '\t'
      << msg.sys_site_id       << '\t'
      << msg.raw_frame         << '\t'
      << msg.meta;
  return rec.str();
}

void P25FrameLogger::log_messages(const std::vector<TrunkMessage> &messages, System *system, int frame_type) {
  if (messages.empty())
    return;
  std::lock_guard<std::mutex> lock(mtx_);
  if (!log_file_.is_open())
    return;
  for (const auto &msg : messages) {
    // Skip timeout and internal control messages
    if (msg.message_type == INVALID_CC_MESSAGE)
      continue;
    log_file_ << format_record(msg, system, frame_type) << '\n';
  }
  log_file_.flush();
}
