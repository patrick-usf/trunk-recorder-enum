#pragma once
#include <fstream>
#include <mutex>
#include <string>
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

  std::ofstream    log_file_;
  std::string      base_path_;    // original configured path
  std::uintmax_t   max_bytes_{50ULL * 1024 * 1024};
  std::uintmax_t   bytes_written_{0};
  mutable std::mutex mtx_;
};
