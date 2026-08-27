// nuarr self-extracting installer stub.
//
// WHY THIS EXISTS: the previous single-exe was built with IExpress, and past
// roughly 200 MB IExpress writes a TRUNCATED cabinet and exits success - the
// 214 MB bundle came out as 133 MB, failing on the first machine that ran it
// with "corrupted Cabinet file". No error at build time, no error code,
// nothing. A packager that silently corrupts near a size the payload will
// certainly grow past is not a packager, so this replaces it with the least
// machinery that can possibly work: a stub the Windows-bundled C# compiler
// builds in a second, with the zip appended to the exe and a 16-byte trailer
// saying where it starts.
//
//   [ stub.exe ][ nuarr-bundle.zip ][ zipStart:8 bytes ][ "NUARRSFX":8 bytes ]
//
// The stub reads its own file, seeks to the offset, streams the zip out to
// TEMP, extracts it with System.IO.Compression (which, unlike the cab codec,
// fails LOUDLY on truncation), and hands over to Setup.cmd. Elevation comes
// from the embedded manifest, so the UAC prompt appears at double-click - the
// same moment every normal installer asks.
using System;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Text;
using System.Threading;
using System.Windows.Forms;

static class NuarrSfx
{
    const string Magic = "NUARRSFX";

    [STAThread]
    static int Main()
    {
        string self = Process.GetCurrentProcess().MainModule.FileName;
        string stage = Path.Combine(Path.GetTempPath(), "nuarr-setup");
        try
        {
            long zipStart, zipLen;
            using (var f = File.OpenRead(self))
            {
                if (f.Length < 16) throw new Exception("no payload behind the stub");
                var tail = new byte[16];
                f.Seek(-16, SeekOrigin.End);
                ReadAll(f, tail);
                if (Encoding.ASCII.GetString(tail, 8, 8) != Magic)
                    throw new Exception("payload marker missing - the download may be incomplete");
                zipStart = BitConverter.ToInt64(tail, 0);
                zipLen = f.Length - 16 - zipStart;
                if (zipStart <= 0 || zipLen <= 0)
                    throw new Exception("payload offset is nonsense - rebuild the installer");
            }

            // A stale half-extract from a previous failed run must not merge
            // with this one - a mixed-version staging folder is exactly the
            // kind of thing that half-works.
            if (Directory.Exists(stage)) Directory.Delete(stage, true);
            Directory.CreateDirectory(stage);

            using (var ui = new ProgressForm())
            {
                Exception worker = null;
                var t = new Thread(() =>
                {
                    try
                    {
                        string zip = Path.Combine(stage, "bundle.zip");
                        using (var src = File.OpenRead(self))
                        using (var dst = File.Create(zip))
                        {
                            src.Seek(zipStart, SeekOrigin.Begin);
                            CopyExactly(src, dst, zipLen);
                        }
                        // ExtractToDirectory throws on a short or corrupt
                        // archive - the loud failure the cab route never gave.
                        ZipFile.ExtractToDirectory(zip, stage);
                        File.Delete(zip);
                    }
                    catch (Exception ex) { worker = ex; }
                    finally { ui.Done(); }
                });
                t.IsBackground = true;
                t.Start();
                Application.Run(ui);
                if (worker != null) throw worker;
            }

            // STRAIGHT INTO THE WIZARD, NO CONSOLE. The first cut ran
            // `cmd.exe /c Setup.cmd`, which parks a black console window
            // behind the wizard for the whole install - Setup.cmd exists for
            // the unpack-the-zip-by-hand path, where its elevation check and
            // pauses earn their keep. This stub already IS elevated (the
            // manifest saw to that), so everything the cmd adds here is that
            // window. -STA because the wizard's folder pickers are WinForms.
            string wizard = Path.Combine(stage, "setup\\Nuarr-Setup.ps1");
            if (!File.Exists(wizard))
                throw new Exception("the archive unpacked but setup\\Nuarr-Setup.ps1 is not in it");

            var psi = new ProcessStartInfo("powershell.exe",
                "-NoProfile -ExecutionPolicy Bypass -STA -WindowStyle Hidden -File \"" + wizard + "\"")
            {
                WorkingDirectory = stage,
                UseShellExecute = false,
                CreateNoWindow = true,
            };
            using (var p = Process.Start(psi))
            {
                p.WaitForExit();
                return p.ExitCode;
            }
        }
        catch (Exception ex)
        {
            MessageBox.Show(
                ex.Message + "\n\nNothing was installed. About 600 MB free on "
                + "the drive holding TEMP is needed while Setup unpacks.",
                "nuarr installer", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        finally
        {
            // Only the staging copy. Whatever Setup installed is at its real
            // home and none of this stub's business.
            try { if (Directory.Exists(stage)) Directory.Delete(stage, true); }
            catch { /* a locked file here is not worth an error dialog */ }
        }
    }

    static void ReadAll(Stream s, byte[] buf)
    {
        int off = 0;
        while (off < buf.Length)
        {
            int n = s.Read(buf, off, buf.Length - off);
            if (n <= 0) throw new EndOfStreamException();
            off += n;
        }
    }

    static void CopyExactly(Stream src, Stream dst, long count)
    {
        var buf = new byte[1 << 20];
        while (count > 0)
        {
            int n = src.Read(buf, 0, (int)Math.Min(buf.Length, count));
            if (n <= 0) throw new EndOfStreamException("payload ended early");
            dst.Write(buf, 0, n);
            count -= n;
        }
    }

    // The 20 seconds of unpacking need a window, or a double-click appears to
    // do nothing and gets double-clicked again - two extractions into one
    // folder being the exact mess the stale-stage delete above guards against.
    sealed class ProgressForm : Form
    {
        public ProgressForm()
        {
            Text = "nuarr Setup";
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false; MinimizeBox = false;
            StartPosition = FormStartPosition.CenterScreen;
            ClientSize = new System.Drawing.Size(360, 84);
            ControlBox = false;
            var l = new Label
            {
                Text = "Unpacking nuarr...",
                Left = 16, Top = 14, Width = 328,
            };
            var bar = new ProgressBar
            {
                Style = ProgressBarStyle.Marquee,
                Left = 16, Top = 42, Width = 328, Height = 20,
                MarqueeAnimationSpeed = 30,
            };
            Controls.Add(l); Controls.Add(bar);
        }
        public void Done()
        {
            if (InvokeRequired) { BeginInvoke((Action)Done); return; }
            Close();
        }
    }
}
