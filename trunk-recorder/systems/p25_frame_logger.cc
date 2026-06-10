#include "p25_frame_logger.h"
#include "system.h"
#include <boost/log/trivial.hpp>
#include <chrono>
#include <cstdio>
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

// ---------------------------------------------------------------------------
// Public interface
// ---------------------------------------------------------------------------

void P25FrameLogger::open(const std::string &path, std::uintmax_t max_bytes) {
  std::lock_guard<std::mutex> lock(mtx_);
  base_path_ = path;
  max_bytes_ = max_bytes;
  bytes_written_ = 0;
  open_file();
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

void P25FrameLogger::log_messages(const std::vector<TrunkMessage> &messages,
                                  System *system, int frame_type) {
  if (messages.empty())
    return;
  std::lock_guard<std::mutex> lock(mtx_);
  if (!log_file_.is_open())
    return;
  for (const auto &msg : messages) {
    if (msg.message_type == INVALID_CC_MESSAGE)
      continue;
    std::string rec = format_record(msg, system, frame_type) + '\n';
    log_file_ << rec;
    bytes_written_ += rec.size();
  }
  log_file_.flush();
  check_roll();
}

// ---------------------------------------------------------------------------
// Private helpers
// ---------------------------------------------------------------------------

void P25FrameLogger::open_file() {
  // Append to existing file so restarts don't clobber history
  log_file_.open(base_path_, std::ios::app);
  if (!log_file_.is_open()) {
    BOOST_LOG_TRIVIAL(error) << "P25FrameLogger: cannot open " << base_path_;
    return;
  }
  // Determine current file size to track bytes_written_ correctly
  log_file_.seekp(0, std::ios::end);
  std::streamoff current_size = log_file_.tellp();
  bytes_written_ = (current_size > 0) ? static_cast<std::uintmax_t>(current_size) : 0;

  if (bytes_written_ == 0)
    write_header();
}

void P25FrameLogger::roll() {
  if (!log_file_.is_open())
    return;

  log_file_.close();

  // Build rolled filename: strip extension, append timestamp, re-add extension
  std::string rolled = base_path_;
  auto dot = rolled.rfind('.');
  std::string suffix = ts_file_suffix();
  if (dot != std::string::npos) {
    rolled = rolled.substr(0, dot) + "_" + suffix + rolled.substr(dot);
  } else {
    rolled = rolled + "_" + suffix;
  }

  if (std::rename(base_path_.c_str(), rolled.c_str()) != 0) {
    BOOST_LOG_TRIVIAL(error) << "P25FrameLogger: roll rename failed: "
                             << base_path_ << " -> " << rolled;
  } else {
    BOOST_LOG_TRIVIAL(info) << "P25FrameLogger: rolled to " << rolled;
  }

  bytes_written_ = 0;
  open_file();
}

void P25FrameLogger::check_roll() {
  if (bytes_written_ >= max_bytes_)
    roll();
}

void P25FrameLogger::write_header() {
  const char *hdr =
      "timestamp\tsys_name\tnac\tduid\tdirection\tframe_type\tmfid\topcode_hex\t"
      "opcode_name\tdecode_status\ttalkgroup\tsource_id\tfreq_mhz\t"
      "emergency\tencrypted\tphase2_tdma\ttdma_slot\twacn\tsys_id\t"
      "rfss_id\tsite_id\traw_frame\tmeta\n";
  log_file_ << hdr;
  bytes_written_ += std::string(hdr).size();
}

// ---------------------------------------------------------------------------
// Timestamp helpers
// ---------------------------------------------------------------------------

std::string P25FrameLogger::ts_now() const {
  using namespace std::chrono;
  auto now = system_clock::now();
  auto ms  = duration_cast<milliseconds>(now.time_since_epoch()) % 1000;
  auto t   = system_clock::to_time_t(now);
  std::tm tm_utc{};
  gmtime_r(&t, &tm_utc);
  char buf[32];
  strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%S", &tm_utc);
  std::ostringstream oss;
  oss << buf << '.' << std::setfill('0') << std::setw(3) << ms.count() << 'Z';
  return oss.str();
}

std::string P25FrameLogger::ts_file_suffix() const {
  using namespace std::chrono;
  auto now = system_clock::now();
  auto t   = system_clock::to_time_t(now);
  std::tm tm_utc{};
  gmtime_r(&t, &tm_utc);
  char buf[24];
  strftime(buf, sizeof(buf), "%Y%m%d_%H%M%SZ", &tm_utc);
  return std::string(buf);
}

// ---------------------------------------------------------------------------
// Decode helpers
// ---------------------------------------------------------------------------

std::string P25FrameLogger::decode_status(const TrunkMessage &msg) const {
  if (msg.message_type == UNKNOWN)
    return "RAW";
  if (!msg.raw_frame.empty())
    return "PARTIAL";
  return "FULL";
}

std::string P25FrameLogger::opcode_name(unsigned long opcode, unsigned long mfid,
                                        int frame_type) const {
  if (frame_type == 19) { // LCW or ESS (type 19 carries both)
    // ESS frames carry algid in the opcode field; all standard algids are > 0x3f
    // (outside the valid 6-bit LCCO range), so this check is unambiguous.
    if (opcode > 0x3f)
      return "ESS_ENC_SYNC";
    switch (opcode) {
      case 0x00: return "LCW_GRP_V_CH_USER";         // Group Voice Channel User
      case 0x01: return "LCW_RESERVED_01";
      case 0x02: return "LCW_GRP_V_CH_UPDATE";        // Group Voice Channel Update
      case 0x03: return "LCW_UU_V_CH_USER";           // Unit to Unit Voice Channel User
      case 0x04: return "LCW_GRP_V_CH_UPDATE_EXP";   // Group Voice Channel Update Explicit
      case 0x05: return "LCW_UU_ANS_REQ";             // Unit to Unit Answer Request
      case 0x08: return "LCW_TEL_INT_V_CH_USER";      // Telephone Interconnect Voice Channel User
      case 0x09: return "LCW_TEL_INT_ANS_REQ";        // Telephone Interconnect Answer Request
      case 0x0f: return "LCW_ENC_PROD_CTRL";          // Encryption Product Control
      case 0x10: return "LCW_ENC_CTRL";               // Encryption Control (Algorithm ID + Key ID)
      case 0x15: return "LCW_CALL_TERM";              // Call Termination / Cancellation
      case 0x16: return "LCW_SNDCP_CH_ANNOUNCE_EXP"; // SNDCP Data Channel Announcement
      case 0x1c: return "LCW_RFSS_STS_BCAST";        // RFSS Status Broadcast
      case 0x1d: return "LCW_NET_STS_BCAST";         // Network Status Broadcast
      case 0x1e: return "LCW_RESERVED_1E";
      case 0x1f: return "LCW_CALL_ALERT";             // Call Alert
      case 0x20: return "LCW_ACK_RSP";               // Acknowledge Response
      case 0x21: return "LCW_EXT_FUNC_CMD";           // Extended Function Command
      case 0x22: return "LCW_EXT_FUNC_CMD_ACK";      // Extended Function Command Acknowledge
      case 0x27: return "LCW_DENY_RSP";              // Deny Response
      case 0x28: return "LCW_GRP_AFF_RSP";           // Group Affiliation Response
      case 0x2b: return "LCW_LOC_REG_RSP";           // Location Registration Response
      case 0x2c: return "LCW_U_REG_RSP";             // Unit Registration Response
      case 0x2f: return "LCW_U_DE_REG_ACK";          // Unit Deregistration Acknowledge
      case 0x30: return "LCW_TDMA_SYNC_BCAST";       // TDMA Synchronization Broadcast
      case 0x34: return "LCW_IDEN_UP_TDMA";          // Identifier Update for TDMA
      case 0x35: return "LCW_TIME_DATE_ANNOUNCE";    // Time and Date Announcement
      case 0x39: return "LCW_SEC_RFSS_BCAST";        // Secondary RFSS Status Broadcast
      case 0x3a: return "LCW_ADJ_STS_BCAST";         // Adjacent Site Status Broadcast
      case 0x3b: return "LCW_NET_STS_BCAST_EXP";     // Network Status Broadcast Explicit
      case 0x3d: return "LCW_IDEN_UP";               // Identifier Update
      default:   return "LCW_UNKNOWN";
    }
  }
  if (frame_type == 22) { // HDU — opcode field carries algid; > 0x3f means encrypted
    if (opcode == 0x80) return "HDU_CLEAR";
    if (opcode >  0x3f) return "HDU_ENC";
    return "HDU_CLEAR"; // algid 0x00..0x3f — treat as clear
  }
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

  if (mfid == 0x90) {
    switch (opcode) {
      case 0x00: return "TSBK_MOT_GRG_ADD_CMD";
      case 0x01: return "TSBK_MOT_GRG_DEL_CMD";
      case 0x02: return "TSBK_MOT_OSP_PATCH_GRP_CH_GRANT";
      case 0x03: return "TSBK_MOT_OSP_PATCH_GRP_CH_GRANT_UPDT";
      case 0x05: return "TSBK_MOT_OSP_TRAFFIC_CH_ID";
      case 0x06: return "TSBK_MOT_GRG_CN_GRANT_UPDT";
      case 0x09: return "TSBK_MOT_OSP_SYSTEM_LOADING";
      case 0x0b: return "TSBK_MOT_UNKNOWN_0B";
      case 0x16: return "TSBK_MOT_UNKNOWN_16";
      default:   return "TSBK_MOT_UNKNOWN";
    }
  }
  if (mfid == 0xA4) {
    switch (opcode) {
      case 0x30: return "TSBK_MACOM_GRG_EXENC_CMD";
      default:   return "TSBK_MACOM_UNKNOWN";
    }
  }

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
    case 0x15: return "TSBK_SNDCP_CH_REQ";
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
    case 0x2f: return "TSBK_U_DE_REG_ACK_ALT";
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

// ---------------------------------------------------------------------------
// Record formatting
// ---------------------------------------------------------------------------

std::string P25FrameLogger::format_record(const TrunkMessage &msg,
                                          System *system, int frame_type) const {
  std::string frame_type_str;
  switch (frame_type) {
    case 7:  frame_type_str = "TSBK";    break;
    case 12: frame_type_str = "MBT";     break;
    case 15: frame_type_str = "TDULC";   break;
    case 18: frame_type_str = "MAC_PDU"; break;
    case 19: frame_type_str = (msg.duid == 0x0a) ? "ESS" : "LCW"; break;
    case 20: frame_type_str = "RAW_PDU"; break;
    case 22: frame_type_str = "HDU";     break;
    default: frame_type_str = std::to_string(frame_type); break;
  }

  std::ostringstream duid_oss;
  duid_oss << "0x" << std::hex << std::setfill('0') << std::setw(2) << (unsigned int)msg.duid;
  std::string duid_str = duid_oss.str();

  std::string dir_str;
  switch (msg.direction) {
    case DIR_OSP: dir_str = "OSP"; break;
    case DIR_ISP: dir_str = "ISP"; break;
    default:      dir_str = "UNK"; break;
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
  rec << ts_now()                   << '\t'
      << sys_name                   << '\t'
      << nac_hex.str()              << '\t'
      << duid_str                   << '\t'
      << dir_str                    << '\t'
      << frame_type_str             << '\t'
      << mfid_hex.str()             << '\t'
      << opcode_hex.str()           << '\t'
      << opcode_name(msg.opcode, msg.mfid, frame_type) << '\t'
      << decode_status(msg)         << '\t'
      << msg.talkgroup              << '\t'
      << msg.source                 << '\t'
      << freq_str.str()             << '\t'
      << (msg.emergency   ? 1 : 0) << '\t'
      << (msg.encrypted   ? 1 : 0) << '\t'
      << (msg.phase2_tdma ? 1 : 0) << '\t'
      << msg.tdma_slot              << '\t'
      << msg.wacn                   << '\t'
      << msg.sys_id                 << '\t'
      << msg.sys_rfss               << '\t'
      << msg.sys_site_id            << '\t'
      << msg.raw_frame              << '\t'
      << msg.meta;
  return rec.str();
}
