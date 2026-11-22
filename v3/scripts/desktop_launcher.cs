using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

internal static class Launcher
{
    [STAThread]
    private static void Main()
    {
        string root = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments), "量化升级版", "btc-paper-lab-v3", "windows-binance-deploy");
        string python = Path.Combine(root, @".venv\Scripts\pythonw.exe");
        string dashboard = Path.Combine(root, @"scripts\local_dashboard.py");
        if (!File.Exists(python) || !File.Exists(dashboard))
        {
            MessageBox.Show("找不到新仓的运行环境或控制台：\n" + root, "BTC 纸盘控制台", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return;
        }
        try
        {
            var info = new ProcessStartInfo(python, "\"" + dashboard + "\"");
            info.WorkingDirectory = root;
            info.UseShellExecute = false;
            info.CreateNoWindow = true;
            Process.Start(info);
        }
        catch (Exception ex)
        {
            MessageBox.Show("控制台启动失败：\n" + ex.Message, "BTC 纸盘控制台", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }
}
