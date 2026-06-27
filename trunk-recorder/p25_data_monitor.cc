/*
 * p25-data-monitor — standalone P25 data channel passive monitor
 *
 * Receives wideband IQ from trunk-recorder's ZMQ publisher, tunes to one or
 * more P25 data channels simultaneously (each via its own
 * xlat_channelizer→demod→decode chain in one GR flowgraph), and logs
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
#include <cerrno>
#include <cstring>
#include <ctime>
#include <fcntl.h>
#include <getopt.h>
#include <iomanip>
#include <iostream>
#include <memory>
#include <signal.h>
#include <string>
#include <sys/stat.h>
#include <unistd.h>
#include <vector>

#include <gnuradio/analog/pll_freqdet_cf.h>
#include <gnuradio/blocks/file_descriptor_sink.h>
#include <gnuradio/blocks/multiply_const.h>
#include <gnuradio/blocks/repeat.h>
#include <gnuradio/fft/window.h>
#include <gnuradio/filter/fir_filter_blk.h>
#include <gnuradio/filter/firdes.h>
#include <gnuradio/msg_queue.h>
#include <gnuradio/message.h>
#include <gnuradio/top_block.h>
#include <gnuradio/zeromq/sub_source.h>

#include "global_structs.h"
#include "gr_blocks/xlat_channelizer.h"
#include "recorders/p25_recorder_decode.h"
#include "recorders/p25_recorder_fsk4_demod.h"
#include "recorders/p25_recorder_qpsk_demod.h"
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
            << " [--freq-table <path>]    CSV freq table (TABLEID,TYPE,BASE,SPACING,OFFSET)\n"
            << " [--nac        <hex>]     Expected NAC (e.g. 0x842); frames with other NACs excluded from decoded/known counts\n"
            << " [--qpsk]                 Use CQPSK demodulator instead of C4FM/FSK4 (Phase 1 downlink, 4800 sps)\n"
            << " [--phase2]               Use H-DQPSK demodulator for P25 Phase 2 TDMA (6000 sps); implies --qpsk\n"
            << " [--baseband-sink <dir>]  Write 48 kHz f32 FM-demod audio to named FIFOs in <dir>\n"
            << "                          for pdu_harness (p25.rs MessageReceiver). FIFOs are named\n"
            << "                          p25_dl_<freq_hz>.f32 and are created automatically via mkfifo.\n";
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
  unsigned long       filter_nac = 0; // 0 = accept any non-zero NAC
  bool                use_qpsk   = false;
  bool                use_phase2 = false;
  std::string         sink_dir;

  static const struct option long_opts[] = {
    { "zmq",           required_argument, 0, 'z' },
    { "center",        required_argument, 0, 'c' },
    { "rate",          required_argument, 0, 'r' },
    { "freq",          required_argument, 0, 'f' },
    { "log",           required_argument, 0, 'l' },
    { "sys-name",      required_argument, 0, 'n' },
    { "freq-table",    required_argument, 0, 't' },
    { "nac",           required_argument, 0, 'a' },
    { "qpsk",          no_argument,       0, 'q' },
    { "phase2",        no_argument,       0, 'P' },
    { "baseband-sink", required_argument, 0, 'b' },
    { 0, 0, 0, 0 }
  };

  int opt, idx;
  while ((opt = getopt_long(argc, argv, "z:c:r:f:l:n:t:a:qPb:", long_opts, &idx)) != -1) {
    switch (opt) {
      case 'z': zmq_addr        = optarg;                      break;
      case 'c': sdr_center      = std::stod(optarg);           break;
      case 'r': sdr_rate        = std::stod(optarg);           break;
      case 'f': data_freqs.push_back(std::stod(optarg));       break;
      case 'l': log_path        = optarg;                      break;
      case 'n': sys_name        = optarg;                      break;
      case 't': freq_table_path = optarg;                      break;
      case 'a': filter_nac      = std::stoul(optarg, nullptr, 0); break;
      case 'q': use_qpsk        = true;                        break;
      case 'P': use_phase2      = true; use_qpsk = true;       break;
      case 'b': sink_dir        = optarg;                      break;
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
        use_phase2 ? xlat_channelizer::phase2_samples_per_symbol : xlat_channelizer::phase1_samples_per_symbol,
        use_phase2 ? xlat_channelizer::phase2_symbol_rate        : xlat_channelizer::phase1_symbol_rate,
        xlat_channelizer::channel_bandwidth,
        sdr_center,
        false);
    xlat->tune_offset(sdr_center - freq);

    ChanChain c;
    c.freq = freq;
    c.rec  = std::make_unique<DataMonitorRecorder>();
    auto decode = make_p25_recorder_decode(c.rec.get(), 0, false);
    c.rx_q = decode->get_rx_queue();

    tb->connect(zmq_src, 0, xlat, 0);

    // Keep demod sptr in scope so the baseband sink can also connect to it.
    p25_recorder_fsk4_demod_sptr fsk4_sptr;
    p25_recorder_qpsk_demod_sptr qpsk_sptr;
    if (use_qpsk) {
      qpsk_sptr = make_p25_recorder_qpsk_demod();
      if (use_phase2) {
        qpsk_sptr->switch_tdma(true);
        decode->switch_tdma(true);
      }
      tb->connect(xlat,      0, qpsk_sptr, 0);
      tb->connect(qpsk_sptr, 0, decode,    0);
    } else {
      fsk4_sptr = make_p25_recorder_fsk4_demod();
      tb->connect(xlat,      0, fsk4_sptr, 0);
      tb->connect(fsk4_sptr, 0, decode,    0);
    }

    // Optional baseband sink: write 48 kHz f32 C4FM baseband to a named FIFO
    // for pdu_harness (p25.rs MessageReceiver::feed(f32)).
    //
    // p25.rs SyncCorrelator expects FM-demodulated float at 48 kHz with
    // smooth transitions between ±1/±3 symbol levels.  We use pll_freqdet_cf
    // on the 24 kHz xlat output (CQPSK "compatible" FM demod) + ZOH ×2.
    if (!sink_dir.empty()) {
      std::string fifo_path = sink_dir + "/p25_dl_" +
                              std::to_string(static_cast<long long>(freq)) + ".f32";

      // Create FIFO if it doesn't already exist.
      if (mkfifo(fifo_path.c_str(), 0666) < 0 && errno != EEXIST) {
        std::cerr << "[p25-data-monitor] mkfifo " << fifo_path
                  << " failed: " << std::strerror(errno) << "\n";
        return 1;
      }

      // O_RDWR avoids blocking on open when no reader is present (Linux).
      // The FIFO remains writable; pdu_harness opens the read end separately.
      int fd = open(fifo_path.c_str(), O_RDWR);
      if (fd < 0) {
        std::cerr << "[p25-data-monitor] open " << fifo_path
                  << " failed: " << std::strerror(errno) << "\n";
        return 1;
      }

      // FM-equivalent baseband for p25.rs (SAMPLE_RATE=48000, SYMBOL_PERIOD=10):
      //
      // CQPSK "compatible" property: ±45°/±135° phase change per symbol ≡
      // ±600/±1800 Hz FM deviation.  pll_freqdet_cf on the raw 24 kHz xlat
      // output gives the same smooth instantaneous-frequency waveform that a
      // real C4FM FM-demod produces — smooth transitions between symbols create
      // the ISI profile that p25.rs's SyncCorrelator needs for a sharp
      // correlation peak.
      //
      // Hard-decision symbol streams (qpsk_sptr at 4800 sps, even after
      // polyphase interpolation) produce a flat-plateau correlation that fires
      // late into the NID, causing BCH to fail on every frame.  Use the PLL
      // path for both QPSK and C4FM networks.
      //
      // Output: 24 kHz → ZOH ×2 → 48 kHz, 10 samples/symbol.
      {
        const double ch_rate    = 4800.0 * 5.0;  // 24000 Hz
        const double sym_rate   = 4800.0;
        const double pi         = M_PI;
        const double fd_hz      = 600.0;
        const double f2r        = pi / (ch_rate / 2.0);  // π/12000

        auto sink_pll = gr::analog::pll_freqdet_cf::make(
            (sym_rate / 2.0 * 1.2) * f2r,
             (3.0 * fd_hz * 1.9) * f2r,
            -(3.0 * fd_hz * 1.9) * f2r);
        auto sink_amp = gr::blocks::multiply_const_ff::make(1.0 / (fd_hz * f2r));

        // 5-tap MA at 24 kHz: group delay = 2 samples (4 at 48 kHz = 0.4 symbol
        // periods).  This is small enough that the SyncDetector fires at the
        // correct position (first sample of NID[0]), matching the Decoder's
        // pos=1 initial state.  A longer LP filter (e.g. 69-tap Kaiser) would
        // add ~34 samples of group delay at 24 kHz, causing the SyncDetector
        // to fire ~68 samples late and producing a systematic 3-4 dibit
        // misalignment in the TSBK body that defeats the Viterbi decoder.
        std::vector<float> sym_taps(5, 0.2f);
        auto sink_sym = gr::filter::fir_filter_fff::make(1, sym_taps);

        auto upsamp   = gr::blocks::repeat::make(sizeof(float), 2);
        auto fd_sink  = gr::blocks::file_descriptor_sink::make(sizeof(float), fd);

        tb->connect(xlat,       0, sink_pll,   0);
        tb->connect(sink_pll,   0, sink_amp,   0);
        tb->connect(sink_amp,   0, sink_sym,   0);
        tb->connect(sink_sym,   0, upsamp,     0);
        tb->connect(upsamp,     0, fd_sink,    0);
      }

      std::cerr << "[p25-data-monitor] baseband sink: " << fifo_path << "\n";
    }

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

  // Per-chain decode-rate counters. Every 60 s, emit a stats line to stderr.
  // "raw"     = all rx_q outputs including timeouts/errors (msg->type() any).
  // "decoded" = msg->type() >= 0 AND returned NAC matches --nac (or any non-zero if --nac omitted).
  // "known"   = decoded AND message_type is a recognized P25 frame type (not UNKNOWN/INVALID).
  struct ChainStats {
    long raw     = 0;
    long decoded = 0;
    long known   = 0;
  };
  std::vector<ChainStats> cstats(chains.size());
  time_t stats_window_start = time(nullptr);
  static const int STATS_INTERVAL = 60; // seconds

  while (running) {
    bool got_msg = false;
    for (size_t i = 0; i < chains.size(); i++) {
      auto &c = chains[i];
      gr::message::sptr msg = c.rx_q->delete_head_nowait();
      if (msg) {
        cstats[i].raw++;
        if (msg->type() >= 0) {
          auto msgs = parser.parse_message(msg, sys, c.freq);
          bool nac_ok = false, type_ok = false;
          for (auto &m : msgs) {
            if (m.nac == 0) continue; // timeout / malformed / TDMA early-return
            if (filter_nac != 0 && m.nac != filter_nac) continue;
            nac_ok = true;
            if (m.message_type != UNKNOWN && m.message_type != INVALID_CC_MESSAGE)
              type_ok = true;
          }
          if (nac_ok)  cstats[i].decoded++;
          if (type_ok) cstats[i].known++;
          got_msg = true;
        }
      }
    }

    // Periodic per-channel stats report
    time_t now = time(nullptr);
    float elapsed = (float)(now - stats_window_start);
    if (elapsed >= STATS_INTERVAL) {
      std::cerr << "[p25-data-monitor stats] " << elapsed << "s window:\n";
      for (size_t i = 0; i < chains.size(); i++) {
        float raw_rate  = cstats[i].raw     / elapsed;
        float dec_rate  = cstats[i].decoded / elapsed;
        float known_rate= cstats[i].known   / elapsed;
        std::cerr << std::fixed << std::setprecision(4)
                  << "  " << (chains[i].freq / 1e6) << " MHz"
                  << "  raw="     << std::setprecision(2) << raw_rate   << "/s"
                  << "  decoded=" << dec_rate  << "/s"
                  << "  known="   << known_rate << "/s"
                  << "  (" << cstats[i].raw << "r "
                  << cstats[i].decoded << "d "
                  << cstats[i].known   << "k)\n";
        cstats[i] = {};
      }
      stats_window_start = now;
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
