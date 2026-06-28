#pragma once
#include <fstream>
#include <mutex>
#include <string>
#include <unordered_set>
#include <vector>
#include "parser.h"
#include "system.h"

class P25FrameLogger {
public:
  static P25FrameLogger &instance();

  // path      — base path for the log file (e.g. "/logs/p25_frames.tsv")
  // max_bytes — roll the file when it reaches this size; default 50 MB
  void open(const std::string &path, std::uintmax_t max_bytes = 50ULL * 1024 * 1024);
  void close();
  bool is_open() const;

  // Log all TrunkMessages from a single decoded frame. frame_type mirrors the
  // GNURadio message type: 7=TSBK, 12=MBT/PDU, 18=Phase2 MAC PDU, 20=raw non-MBT PDU.
  void log_messages(const std::vector<TrunkMessage> &messages, System *system, int frame_type);

  // Quiet mode: suppress repeated writes of static broadcast opcodes (RFSS_STS,
  // NET_STS, ADJ_STS, IDEN_UP, SCCB, etc.). Each unique (opcode + meta content)
  // pair is written only once; identical repeats are silently dropped. Content
  // changes (e.g. CC failover) are logged because they produce a new unique key.
  // The seen-set is cleared on every file roll so each new file captures the
  // current broadcast state. Grants, registrations, data, and unknown frames
  // are never suppressed regardless of quiet mode.
  void set_quiet_mode(bool enable);

private:
  P25FrameLogger() = default;
  ~P25FrameLogger();
  P25FrameLogger(const P25FrameLogger &) = delete;
  P25FrameLogger &operator=(const P25FrameLogger &) = delete;

  void open_file();          // open/create base_path_, write header if empty
  void roll();               // rename current file to timestamped name, open fresh
  void check_roll();         // call after each write; rolls if size >= max_bytes_
  void write_header();

  std::string format_record(const TrunkMessage &msg, System *system, int frame_type) const;
  std::string opcode_name(unsigned long opcode, unsigned long mfid, int frame_type) const;
  std::string decode_status(const TrunkMessage &msg) const;
  std::string ts_now() const;
  std::string ts_file_suffix() const; // UTC timestamp string safe for filenames

  // Returns true for opcodes that broadcast static site/system information and
  // repeat with identical content many times per minute on the control channel.
  static bool is_broadcast_opcode(unsigned long opcode, unsigned long mfid, int frame_type);

  std::ofstream    log_file_;
  std::string      base_path_;    // original configured path
  std::uintmax_t   max_bytes_{50ULL * 1024 * 1024};
  std::uintmax_t   bytes_written_{0};
  mutable std::mutex mtx_;

  // quiet-mode state (guarded by mtx_)
  bool quiet_mode_{false};
  std::unordered_set<std::string> quiet_seen_; // opcode|mfid|ft|meta keys already written
};
