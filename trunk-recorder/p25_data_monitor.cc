/*
 * p25-data-monitor — standalone P25 data channel passive monitor
 *
 * Receives wideband IQ from trunk-recorder's ZMQ publisher, tunes to one or
 * more P25 Phase-1 data channels simultaneously (each via its own
 * xlat_channelizer→fsk4_demod→decode chain in one GR flowgraph), and logs
 * all parsed frames to a single TSV file via P25FrameLogger.  The freq_mhz
 * column in the log distinguishes which channel each frame came from.
 *
 * Usage:
 *   p25-data-monitor --zmq tcp://127.0.0.1:5556 \
 *                   --center 856500000           \
 *                   --rate   10800000            \
 *                   --freq   851575000           \
 *                   --freq   851687500           \
 *                   --freq   851987500           \
 *                   --freq   852112500           \
 *                   --freq   852800000           \
 *                   --freq   853062500           \
 *                   --log    /logs/p25_data.tsv  \
 *                   [--sys-name HowardCounty]
 *
 * Multiple --freq flags build one channelizer chain per frequency.
 */

#include <atomic>
#include <getopt.h>
#include <iostream>
#include <memory>
#include <signal.h>
#include <string>
#include <unistd.h>
#include <vector>

#include <gnuradio/msg_queue.h>
#include <gnuradio/message.h>
#include <gnuradio/top_block.h>
#include <gnuradio/zeromq/sub_source.h>

#include "global_structs.h"
#include "gr_blocks/xlat_channelizer.h"
#include "recorders/p25_recorder_decode.h"
#include "recorders/p25_recorder_fsk4_demod.h"
#include "recorders/recorder.h"
#include "systems/p25_frame_logger.h"
#include "systems/p25_parser.h"
#include "systems/system.h"

static std::atomic<bool> running{true};

static void sig_handler(int) { running = false; }

// Minimal Recorder stub: p25_recorder_decode only calls
// get_enable_audio_streaming() during initialization.
class DataMonitorRecorder : public Recorder {
public:
  DataMonitorRecorder() : Recorder(P25) {
    recording_count          = 0;
    recording_duration       = 0.0;
    d_enable_audio_streaming = false;
    conventional             = false;
    selector_port            = 0;
    rec_num                  = 0;
  }
};

static void print_usage(const char *prog) {
  std::cerr << "Usage: " << prog << "\n"
            << "  --zmq        <address>  ZMQ SUB address (e.g. tcp://127.0.0.1:5556)\n"
            << "  --center     <hz>       SDR center frequency in Hz\n"
            << "  --rate       <sps>      SDR sample rate in samples/s\n"
            << "  --freq       <hz>       Data channel frequency in Hz (repeat for each channel)\n"
            << "  --log        <path>     Output TSV log file path\n"
            << " [--sys-name   <name>]    System short name for log (default: data-monitor)\n"
            << " [--freq-table <path>]    CSV freq table (TABLEID,TYPE,BASE,SPACING,OFFSET)\n";
}

// Per-channel state held for the lifetime of the flowgraph.
struct ChanChain {
  double                              freq;
  std::unique_ptr<DataMonitorRecorder> rec;
  gr::msg_queue::sptr                 rx_q;
};

int main(int argc, char **argv) {
  signal(SIGINT,  sig_handler);
  signal(SIGTERM, sig_handler);

  std::string         zmq_addr;
  double              sdr_center = 0.0;
  double              sdr_rate   = 0.0;
  std::vector<double> data_freqs;
  std::string         log_path;
  std::string         sys_name = "data-monitor";
  std::string         freq_table_path;

  static const struct option long_opts[] = {
    { "zmq",        required_argument, 0, 'z' },
    { "center",     required_argument, 0, 'c' },
    { "rate",       required_argument, 0, 'r' },
    { "freq",       required_argument, 0, 'f' },
    { "log",        required_argument, 0, 'l' },
    { "sys-name",   required_argument, 0, 'n' },
    { "freq-table", required_argument, 0, 't' },
    { 0, 0, 0, 0 }
  };

  int opt, idx;
  while ((opt = getopt_long(argc, argv, "z:c:r:f:l:n:t:", long_opts, &idx)) != -1) {
    switch (opt) {
      case 'z': zmq_addr        = optarg;                      break;
      case 'c': sdr_center      = std::stod(optarg);           break;
      case 'r': sdr_rate        = std::stod(optarg);           break;
      case 'f': data_freqs.push_back(std::stod(optarg));       break;
      case 'l': log_path        = optarg;                      break;
      case 'n': sys_name        = optarg;                      break;
      case 't': freq_table_path = optarg;                      break;
      default:
        print_usage(argv[0]);
        return 1;
    }
  }

  if (zmq_addr.empty() || sdr_center == 0.0 || sdr_rate == 0.0 ||
      data_freqs.empty() || log_path.empty()) {
    print_usage(argv[0]);
    return 1;
  }

  System *sys = System::make(0);
  sys->set_short_name(sys_name);
  sys->set_system_type("p25");

  P25FrameLogger::instance().open(log_path);
  if (!P25FrameLogger::instance().is_open()) {
    std::cerr << "[p25-data-monitor] ERROR: cannot open log file: " << log_path << "\n";
    delete sys;
    return 1;
  }

  // ── Build GR flowgraph ──────────────────────────────────────────────────
  gr::top_block_sptr tb = gr::make_top_block("p25-data-monitor");

  // One ZMQ subscriber receives the wideband IQ from trunk-recorder.
  // GR fan-out distributes samples to all downstream channelizers.
  auto zmq_src = gr::zeromq::sub_source::make(
      sizeof(gr_complex), 1,
      const_cast<char *>(zmq_addr.c_str()));

  // Build one xlat→fsk4→decode chain per requested frequency.
  std::vector<ChanChain> chains;
  chains.reserve(data_freqs.size());

  for (double freq : data_freqs) {
    auto xlat = xlat_channelizer::make(
        sdr_rate,
        xlat_channelizer::phase1_samples_per_symbol,
        xlat_channelizer::phase1_symbol_rate,
        xlat_channelizer::channel_bandwidth,
        sdr_center,
        false);
    xlat->tune_offset(sdr_center - freq);

    auto fsk4 = make_p25_recorder_fsk4_demod();

    ChanChain c;
    c.freq = freq;
    c.rec  = std::make_unique<DataMonitorRecorder>();
    auto decode = make_p25_recorder_decode(c.rec.get(), 0, false);
    c.rx_q = decode->get_rx_queue();

    tb->connect(zmq_src, 0, xlat,   0);
    tb->connect(xlat,    0, fsk4,   0);
    tb->connect(fsk4,    0, decode, 0);

    chains.push_back(std::move(c));
  }

  tb->start();

  std::cerr << "[p25-data-monitor] running"
            << "  zmq="    << zmq_addr
            << "  center=" << sdr_center
            << "  log="    << log_path << "\n";
  for (auto &c : chains)
    std::cerr << "  channel: " << c.freq << " Hz\n";

  // ── Polling loop ─────────────────────────────────────────────────────────
  // Single-threaded: drain each chain's rx_queue in round-robin.
  // P25Parser and P25FrameLogger are not touched concurrently.
  P25Parser parser;
  if (!freq_table_path.empty())
    parser.load_freq_table(freq_table_path, sys->get_sys_num());

  while (running) {
    bool got_msg = false;
    for (auto &c : chains) {
      gr::message::sptr msg = c.rx_q->delete_head_nowait();
      if (msg && msg->type() >= 0) {
        parser.parse_message(msg, sys, c.freq);
        got_msg = true;
      }
    }
    if (!got_msg)
      usleep(5000);
  }

  std::cerr << "[p25-data-monitor] shutting down\n";
  tb->stop();
  tb->wait();
  P25FrameLogger::instance().close();
  delete sys;
  return 0;
}
