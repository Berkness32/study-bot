#!/usr/bin/env node
import initSqlJs  from 'sql.js';
import chalk      from 'chalk';
import { readFileSync, existsSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join }  from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname  = dirname(__filename);
const DB_PATH    = join(__dirname, 'data', 'job-apps', 'applications.db');

if (!existsSync(DB_PATH)) {
    console.log(chalk.dim('No applications database found. Run the job agent to log applications.'));
    process.exit(0);
}

const SQL = await initSqlJs();
const db  = new SQL.Database(readFileSync(DB_PATH));

const result = db.exec(`
    SELECT id, date_applied, job_title, company, job_board,
           pay, address, easy_apply, status
    FROM applications
    ORDER BY id DESC
`);

if (!result.length || !result[0].values.length) {
    console.log(chalk.dim('No applications logged yet.'));
    process.exit(0);
}

const columns = result[0].columns;
const rows    = result[0].values.map(row =>
    Object.fromEntries(columns.map((col, i) => [col, row[i]]))
);

// Column widths
const COL = {
    num:     4,
    date:    12,
    title:   28,
    company: 22,
    board:   15,
    pay:     22,
    loc:     18,
    type:    13,
    status:  13,
};

function pad(str, len) {
    str = String(str ?? '');
    return str.length > len ? str.slice(0, len - 1) + '…' : str.padEnd(len);
}

function colorStatus(status) {
    switch (status) {
        case 'interviewing': return chalk.bold.yellow(pad(status, COL.status));
        case 'offer':        return chalk.bold.green(pad(status, COL.status));
        case 'rejected':     return chalk.dim.red(pad(status, COL.status));
        default:             return chalk.white(pad(status ?? 'applied', COL.status));
    }
}

const colorBoard = board => chalk.blue(pad(board ?? 'direct', COL.board));
const colorPay   = pay   => (!pay || pay === 'Not listed')
    ? chalk.dim('—'.padEnd(COL.pay))
    : chalk.green(pad(pay, COL.pay));
const colorLoc   = addr  => addr
    ? chalk.white(pad(addr, COL.loc))
    : chalk.dim('—'.padEnd(COL.loc));
const colorType  = easy  => easy
    ? chalk.magenta(pad('⚡ Easy Apply', COL.type))
    : chalk.white(pad('Full App', COL.type));

const header = [
    chalk.dim(pad('#',         COL.num)),
    chalk.dim(pad('Date',      COL.date)),
    chalk.dim(pad('Job Title', COL.title)),
    chalk.dim(pad('Company',   COL.company)),
    chalk.dim(pad('Board',     COL.board)),
    chalk.dim(pad('Pay',       COL.pay)),
    chalk.dim(pad('Location',  COL.loc)),
    chalk.dim(pad('Type',      COL.type)),
    chalk.dim(pad('Status',    COL.status)),
].join(' ');

const totalWidth = Object.values(COL).reduce((a, b) => a + b, 0) + Object.keys(COL).length - 1;
const divider    = chalk.dim('─'.repeat(totalWidth));

console.log();
console.log(header);
console.log(divider);

rows.forEach((row, i) => {
    console.log([
        chalk.dim(pad(i + 1,          COL.num)),
        chalk.yellow(pad(row.date_applied, COL.date)),
        chalk.bold.white(pad(row.job_title, COL.title)),
        chalk.cyan(pad(row.company,   COL.company)),
        colorBoard(row.job_board),
        colorPay(row.pay),
        colorLoc(row.address),
        colorType(row.easy_apply),
        colorStatus(row.status),
    ].join(' '));
});

// Weekly / monthly counts
const now          = new Date();
const daysSinceMon = (now.getDay() + 6) % 7;
const weekStart    = new Date(now);
weekStart.setDate(now.getDate() - daysSinceMon);
const weekStartStr  = weekStart.toISOString().slice(0, 10);
const monthStartStr = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-01`;

const weekCount  = rows.filter(r => r.date_applied >= weekStartStr).length;
const monthCount = rows.filter(r => r.date_applied >= monthStartStr).length;

console.log(divider);
console.log(`  ${chalk.dim('Applied this week  :')}  ${chalk.bold.white(weekCount)}`);
console.log(`  ${chalk.dim('Applied this month :')}  ${chalk.bold.white(monthCount)}`);
console.log(divider);
console.log();
